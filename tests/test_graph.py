import json
from types import SimpleNamespace

from langgraph.types import Command

from agent.audit import AuditLog
from agent.config import Settings
from agent.github.client import FakeGitHubClient
from agent.graph import build_graph
from agent.rag.retriever import KeywordRetriever

S = Settings(repo_allowlist=("acme/app",))
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
LOG = f"npm ERR! missing script: build\ncheckout token {TOKEN}\n"
RUNBOOK = KeywordRetriever({"ci.md#Missing build script": "When npm reports missing script build, add a build script to package.json"})
ORIGINAL = '{\n  "name": "x",\n  "scripts": {\n    "test": "echo ok"\n  }\n}\n'
FILES = {"package.json": ORIGINAL}
EDIT = {"path": "package.json", "find": '"test": "echo ok"', "replace": '"test": "echo ok",\n    "build": "echo b"'}
PROPOSAL = {"summary": "add build script", "runbook_section": "ci.md#Missing build script", "edits": [EDIT]}


class FakeLLM:
    def __init__(self, proposal):
        self.proposal, self.seen = proposal, []

    def invoke(self, messages):
        self.seen.append(str(messages))
        if "Summarize" in str(messages[0]):
            return SimpleNamespace(content="npm missing script build in package.json")
        return SimpleNamespace(content=json.dumps(self.proposal))


def run(proposal=PROPOSAL, runbook=RUNBOOK):
    gh, llm, audit = FakeGitHubClient(logs=LOG, files=FILES), FakeLLM(proposal), AuditLog()
    g = build_graph(llm, gh, runbook, S, audit)
    cfg = {"configurable": {"thread_id": "t"}}
    g.invoke({"repo": "acme/app", "run_id": 7, "triggered_by": "alice"}, cfg)
    return g, cfg, gh, llm, audit


def decision(g, cfg, **kw):
    d = {"approved": True, "approver_sub": "bob", "permissions": ["runs:approve"],
         "diff_hash": g.get_state(cfg).values["diff_hash"]} | kw
    return g.invoke(Command(resume=d), cfg)


def test_pauses_before_execution_and_never_leaks_token():
    g, cfg, gh, llm, _ = run()
    assert g.get_state(cfg).values["status"] == "awaiting_approval"
    assert not any(c[0] in ("merge", "rerun") for c in gh.calls)
    assert all(TOKEN not in m for m in llm.seen)


def test_approved_executes():
    g, cfg, gh, *_ = run()
    assert decision(g, cfg)["status"] == "executed"
    assert [c[0] for c in gh.calls][-2:] == ["merge", "rerun"]


def test_self_approval_blocked():
    g, cfg, gh, *_ = run()
    assert decision(g, cfg, approver_sub="alice")["status"] == "rejected"
    assert not any(c[0] == "merge" for c in gh.calls)


def test_tampered_diff_blocked():
    g, cfg, gh, *_ = run()
    assert decision(g, cfg, diff_hash="deadbeef")["status"] == "rejected"


def test_missing_permission_blocked():
    g, cfg, gh, *_ = run()
    assert decision(g, cfg, permissions=["runs:read"])["status"] == "rejected"


def test_rejection_does_not_execute():
    g, cfg, gh, *_ = run()
    assert decision(g, cfg, approved=False)["status"] == "rejected"
    assert not any(c[0] == "merge" for c in gh.calls)


def test_no_runbook_means_needs_human_no_pr():
    g, cfg, gh, *_ = run(runbook=KeywordRetriever({}))
    assert g.get_state(cfg).values["status"] == "needs_human"
    assert not any(c[0] == "pr" for c in gh.calls)


def test_uncited_or_out_of_policy_fix_never_opens_pr():
    bad = PROPOSAL | {"runbook_section": "made-up"}
    g, cfg, gh, *_ = run(proposal=bad)
    assert g.get_state(cfg).values["status"] == "needs_human"
    bad2 = PROPOSAL | {"edits": [{"path": "src/secret.py", "find": "a", "replace": "b"}]}
    g, cfg, gh, *_ = run(proposal=bad2)
    assert not any(c[0] == "pr" for c in gh.calls)


class StubGuard:
    def __init__(self, block_on=None):
        self.block_on, self.calls = block_on, []

    def check(self, text, source="INPUT"):
        from agent.guard.bedrock import GuardrailBlocked

        self.calls.append(source)
        if source == self.block_on:
            raise GuardrailBlocked("blocked by guardrail")


def run_guarded(block_on):
    gh, guard = FakeGitHubClient(logs=LOG, files=FILES), StubGuard(block_on)
    g = build_graph(FakeLLM(PROPOSAL), gh, RUNBOOK, S, AuditLog(), guard=guard)
    cfg = {"configurable": {"thread_id": "t"}}
    g.invoke({"repo": "acme/app", "run_id": 7, "triggered_by": "alice"}, cfg)
    return g, cfg, gh, guard


def test_input_guardrail_block_stops_before_llm_and_pr():
    g, cfg, gh, guard = run_guarded("INPUT")
    assert g.get_state(cfg).values["status"] == "needs_human"
    assert not any(c[0] == "pr" for c in gh.calls)


def test_output_guardrail_block_means_no_pr():
    g, cfg, gh, guard = run_guarded("OUTPUT")
    assert guard.calls == ["INPUT", "OUTPUT"]
    assert g.get_state(cfg).values["status"] == "needs_human"
    assert not any(c[0] == "pr" for c in gh.calls)


def test_guardrail_fails_closed_and_refuses_draft():
    import pytest

    from agent.guard.bedrock import BedrockGuard, GuardrailBlocked

    class Boom:
        def apply_guardrail(self, **kw):
            raise RuntimeError("aws down")

    with pytest.raises(GuardrailBlocked):
        BedrockGuard("gr", "1", "us-west-2", client=Boom()).check("x")
    with pytest.raises(ValueError):
        BedrockGuard("gr", "DRAFT", "us-west-2", client=Boom())


def test_state_survives_restart(tmp_path):
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    db = str(tmp_path / "s.db")
    gh = FakeGitHubClient(logs=LOG, files=FILES)
    cfg = {"configurable": {"thread_id": "t"}}
    g1 = build_graph(FakeLLM(PROPOSAL), gh, RUNBOOK, S, checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    g1.invoke({"repo": "acme/app", "run_id": 7, "triggered_by": "alice"}, cfg)
    g2 = build_graph(FakeLLM(PROPOSAL), gh, RUNBOOK, S, checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    assert g2.get_state(cfg).values["status"] == "awaiting_approval"
    assert decision(g2, cfg)["status"] == "executed"


def test_parse_json_tolerates_fences_and_prose():
    from agent.graph import parse_json
    assert parse_json('Here:\n```json\n{"a": 1}\n```') == {"a": 1}
    import pytest
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_citation_must_match_source_or_real_heading():
    from agent.graph import cites_retrieved
    rb = [{"section": "s3://b/ci.md", "text": "# T\n## Missing build script\nadd it"}]
    assert cites_retrieved("s3://b/ci.md", rb)
    assert cites_retrieved("Missing build script", rb)
    assert not cites_retrieved("Invented section", rb)
    assert not cites_retrieved("", rb) and not cites_retrieved(None, rb)


def test_citation_with_hash_marks_is_normalized_but_still_verified():
    from agent.graph import cites_retrieved
    rb = [{"section": "s3://b/ci.md", "text": "# T\n## Missing build script\nadd it"}]
    assert cites_retrieved("## Missing build script", rb)
    assert not cites_retrieved("## Invented", rb) and not cites_retrieved("###", rb)


def test_server_computes_diff_and_rejects_bad_edits():
    cases = {
        "ok": ([EDIT], "awaiting_approval"),
        "missing target": ([EDIT | {"path": "nope.json"}], "needs_human"),
        "find absent": ([EDIT | {"find": "not in file"}], "needs_human"),
        "find ambiguous": ([EDIT | {"find": '"'}], "needs_human"),
        "deletes too much": ([EDIT | {"find": '"name": "x",\n  "scripts": {\n    "test": "echo ok"', "replace": '"scripts": {'}], "needs_human"),
        "out of policy path": ([{"path": "src/app.py", "find": "a", "replace": "b"}], "needs_human"),
    }
    for name, (edits, expect) in cases.items():
        gh = FakeGitHubClient(logs=LOG, files=FILES | {"src/app.py": "a"})
        g = build_graph(FakeLLM(PROPOSAL | {"edits": edits, "diff": "+lie"}), gh, RUNBOOK, S, AuditLog())
        cfg = {"configurable": {"thread_id": name}}
        g.invoke({"repo": "acme/app", "run_id": 7, "triggered_by": "alice"}, cfg)
        v = g.get_state(cfg).values
        assert v["status"] == expect, (name, v.get("error"))
        if expect == "awaiting_approval":
            d = v["proposal"]["changes"][0]["diff"]
            assert '+    "build": "echo b"' in d and "lie" not in d
        else:
            assert not any(c[0] == "pr" for c in gh.calls), name

