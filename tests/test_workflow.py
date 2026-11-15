"""The Temporal workflow against fakes, in a real (in-process) Temporal server."""
import json

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from agent.llm import ScriptedLLM
from agent.rag.retriever import KeywordRetriever
from agent.temporal.models import RunView
from agent.temporal.workflows import FixRunWorkflow
from tests.helpers import TOKEN, Guard, harness, new_github


def calls(gh, kind):
    return [c for c in gh.calls if c[0] == kind]


async def test_happy_path_pauses_then_merges_and_reruns():
    async with harness() as t:
        h = await t.start()
        v = await t.until(h, "awaiting_approval")
        assert v.pr.number == 1 and v.diff_hash and '"build": "echo build ok"' in v.proposal["changes"][0]["diff"]
        assert not calls(t.gh, "merge") and not calls(t.gh, "rerun")  # nothing executes before a human says so
        assert (await t.decide(h)).outcome == "approved"
        out = await h.result()
        assert out.status == "executed" and [c[0] for c in t.gh.calls][-2:] == ["merge", "rerun"]
        assert t.gh.calls[-2][3] == "sha-abc"  # merge pinned to the head SHA that was approved
        assert [e["event"] for e in out.audit] == ["triggered", "pr_opened", "decision", "fix_executed"]


@pytest.mark.parametrize("kw,why", [
    ({"approver_sub": "alice"}, "self"),
    ({"diff_hash": "deadbeef"}, "hash"),
    ({"permissions": ["runs:read"]}, "perm"),
])
async def test_bad_decisions_are_denied_and_the_run_keeps_waiting(kw, why):
    async with harness() as t:
        h = await t.start()
        await t.until(h, "awaiting_approval")
        r = await t.decide(h, **kw)
        assert r.outcome == "denied", why
        assert (await h.query(FixRunWorkflow.view)).status == "awaiting_approval"
        assert not calls(t.gh, "merge")
        assert (await t.decide(h)).outcome == "approved"  # a legitimate approver can still act afterwards
        out = await h.result()
        assert out.status == "executed"
        assert any(e["event"] in ("self_approval_blocked", "tampered_diff_blocked", "decision_denied") for e in out.audit)


async def test_reject_closes_the_pr_and_never_merges():
    async with harness() as t:
        h = await t.start()
        await t.until(h, "awaiting_approval")
        assert (await t.decide(h, approved=False)).outcome == "rejected"
        out = await h.result()
        assert out.status == "rejected" and calls(t.gh, "close") and not calls(t.gh, "merge")


async def test_second_decision_is_refused_by_the_validator():
    async with harness() as t:
        h = await t.start()
        await t.until(h, "awaiting_approval")
        await t.decide(h)
        with pytest.raises((WorkflowUpdateFailedError, RPCError)):  # rejected by the validator, or the run already finished
            await t.decide(h)


async def test_approval_timeout_closes_pr_and_needs_human():
    async with harness() as t:
        h = await t.start(ttl=2)  # a 2 second human deadline
        out = await h.result()
        assert out.status == "needs_human" and "No decision" in out.error
        assert calls(t.gh, "close") and not calls(t.gh, "merge")
        assert "pr_closed" in [e["event"] for e in out.audit]


async def test_transient_github_failure_is_retried_without_duplicate_pr():
    async with harness(gh=new_github(fail_open=2)) as t:
        h = await t.start()
        v = await t.until(h, "awaiting_approval")
        assert len(calls(t.gh, "pr-attempt")) == 3 and len(calls(t.gh, "pr")) == 1 and v.pr.number == 1


async def test_no_runbook_match_means_needs_human_no_pr():
    async with harness(retriever=KeywordRetriever({})) as t:
        out = await (await t.start()).result()
        assert out.status == "needs_human" and not calls(t.gh, "pr")


@pytest.mark.parametrize("block_on", ["INPUT", "OUTPUT"])
async def test_guardrail_block_means_no_pr(block_on):
    async with harness(guard=Guard(block_on)) as t:
        out = await (await t.start()).result()
        assert out.status == "needs_human" and "guardrail" in out.error and not calls(t.gh, "pr")


class BadModel(ScriptedLLM):
    def __init__(self, proposal):
        self.p = proposal

    def complete(self, system, user, json_mode=False):
        return json.dumps(self.p) if json_mode else "npm missing script build in package.json"


EDIT = {"path": "package.json", "find": '"test": "echo ok"', "replace": '"test": "echo ok",\n    "build": "echo b"'}
OK = {"summary": "s", "runbook_section": "ci.md#Missing build script", "edits": [EDIT]}


@pytest.mark.parametrize("name,proposal", [
    ("uncited", OK | {"runbook_section": "made-up"}),
    ("missing target", OK | {"edits": [EDIT | {"path": "nope.json"}]}),
    ("find absent", OK | {"edits": [EDIT | {"find": "not in file"}]}),
    ("find ambiguous", OK | {"edits": [EDIT | {"find": '"'}]}),
    ("deletes too much", OK | {"edits": [EDIT | {"find": '"name": "x",\n  "scripts": {\n    "test": "echo ok"', "replace": '"scripts": {'}]}),
    ("out of policy path", OK | {"edits": [{"path": "src/app.py", "find": "a", "replace": "b"}]}),
    ("no edits", {"summary": "nothing applies", "edits": []}),
])
async def test_unsafe_or_unusable_model_output_never_opens_a_pr(name, proposal):
    async with harness(llm=BadModel(proposal)) as t:
        out = await (await t.start()).result()
        assert out.status == "needs_human", (name, out.error)
        assert "No runbook" not in out.error, name  # it reached the proposal step and was refused there
        assert not calls(t.gh, "pr"), name


async def test_model_diff_is_ignored_server_computes_it():
    async with harness(llm=BadModel(OK | {"diff": "+lie"})) as t:
        v = await t.until(await t.start(), "awaiting_approval")
        assert "lie" not in v.proposal["changes"][0]["diff"]


async def test_workflow_history_is_ciphertext_and_never_contains_the_token():
    async with harness() as t:
        h = await t.start()
        await t.until(h, "awaiting_approval")
        hist = await h.fetch_history()
        blob = json.dumps(hist.to_json_dict()) if hasattr(hist, "to_json_dict") else hist.to_json()
        assert TOKEN not in blob and "build ok" not in blob and "missing script" not in blob  # encrypted at rest
        assert "binary/encrypted" in blob or "YmluYXJ5L2VuY3J5cHRlZA" in blob


async def test_replay_of_a_real_history_is_deterministic():
    from temporalio.worker import Replayer

    async with harness() as t:
        h = await t.start()
        await t.until(h, "awaiting_approval")
        await t.decide(h)
        await h.result()
        hist = await h.fetch_history()
    await Replayer(workflows=[FixRunWorkflow], data_converter=__import__("agent.temporal.codec", fromlist=["x"]).data_converter(
        __import__("tests.helpers", fromlist=["x"]).KEY)).replay_workflow(hist)


def test_view_model_round_trips():
    assert RunView.model_validate(RunView(id="a", repo="r", triggered_by="t").model_dump()).status == "analyzing"


async def test_invalid_syntax_is_rejected_after_one_feedback_retry():
    """The real-world case: a model that duplicates a key and drops a comma. The checker refuses it and the model gets one retry."""
    broken = OK | {"edits": [EDIT | {"replace": '"test": "echo ok"\n    "build": "x"'}]}

    class Model(BadModel):
        calls = 0

        def complete(self, system, user, json_mode=False):
            if json_mode:
                Model.calls += 1
                if Model.calls == 2:
                    assert "REJECTED by the checker" in user and "no longer parses" in user
            return super().complete(system, user, json_mode)

    async with harness(llm=Model(broken)) as t:
        out = await (await t.start()).result()
        assert out.status == "needs_human" and "no longer parses" in out.error and not calls(t.gh, "pr")
        assert Model.calls == 2


async def test_model_that_fixes_itself_after_feedback_gets_a_pr():
    bad = OK | {"edits": [EDIT | {"replace": '"test": "echo ok"\n    "build": "x"'}]}

    class Model(BadModel):
        def __init__(self):
            self.n = 0

        def complete(self, system, user, json_mode=False):
            if not json_mode:
                return "npm missing script build in package.json"
            self.n += 1
            return json.dumps(bad if self.n == 1 else OK)

    async with harness(llm=Model()) as t:
        v = await t.until(await t.start(), "awaiting_approval")
        assert '"build": "echo b"' in v.proposal["changes"][0]["diff"]
