"""Activities: every side effect (GitHub, model, guardrail) lives here, never in workflow code.

Failure policy:
  * transient (network, 5xx, 429)         -> raise, Temporal retries with backoff
  * permanent (4xx, allowlist, bad config) -> non-retryable ApplicationError
  * harness decisions (guard block, policy violation, unusable model output) -> returned as data;
    the workflow ends the run as needs_human. These are results, not errors."""
import functools

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent import steps
from agent.config import Settings
from agent.github.client import GitHubClient
from agent.guard.bedrock import GuardrailBlocked
from agent.guard.policy import FileChange, PolicyViolation
from agent.guard.redact import redact
from agent.llm import LLM
from agent.rag.retriever import Retriever
from agent.temporal.models import ClosePrIn, ExecIn, LogsOut, PrIn, ProposeIn, ProposeOut, PrOut


def _classify(fn):
    """Turn permanent failures into non-retryable errors so Temporal does not hammer GitHub with a doomed call."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except PolicyViolation as e:  # allowlist / branch prefix: configuration fault, not transient
            raise ApplicationError(str(e), type="PolicyViolation", non_retryable=True) from e
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if 400 <= code < 500 and code not in (408, 409, 425, 429):
                raise ApplicationError(f"GitHub {code} for {e.request.method} {e.request.url.path}",
                                       type="GitHubClientError", non_retryable=True) from e
            raise

    return wrapper


MAX_PROPOSALS = 2


class CiActivities:
    def __init__(self, llm: LLM, github: GitHubClient, retriever: Retriever, settings: Settings, guard=None):
        self.llm, self.gh, self.retriever, self.s, self.guard = llm, github, retriever, settings, guard

    def _guard(self, text: str, source: str) -> str:
        if self.guard is None:
            return ""
        try:
            self.guard.check(text, source)
        except GuardrailBlocked as e:
            return str(e)
        return ""

    @activity.defn
    @_classify
    def fetch_logs(self, repo: str, run_id: int) -> LogsOut:
        logs = redact(self.gh.failed_run_logs(repo, run_id))[-20_000:]  # redacted BEFORE it can enter history
        if (err := self._guard(logs, "INPUT")):  # prompt-injection / unsafe content in untrusted logs
            return LogsOut(blocked=err)
        return LogsOut(logs=logs)

    @activity.defn
    def analyze(self, logs: str) -> str:
        return redact(self.llm.complete(steps.SUMMARIZE, logs[-20_000:]))

    @activity.defn
    def retrieve_runbook(self, analysis: str, logs: str = "") -> list[dict]:
        errors = "\n".join(ln for ln in logs.splitlines() if "error" in ln.lower() or "missing" in ln.lower())[-1500:]
        return [{"section": h.section, "text": h.text} for h in self.retriever.retrieve(f"{analysis}\n{errors}")]

    @activity.defn
    @_classify
    def propose_fix(self, i: ProposeIn) -> ProposeOut:
        cache: dict[str, str | None] = {}

        def read(path: str) -> str | None:
            if path not in cache:
                cache[path] = self.gh.file_content(i.repo, path, i.base)
            return cache[path]

        current = {f: read(f) for f in steps.CANDIDATE_FILES}
        prompt = steps.build_prompt(i.analysis, i.runbook, current, i.logs)
        last_err = ""
        for _attempt in range(MAX_PROPOSALS):  # feedback loop: tell the model exactly why a proposal was refused, once
            raw = self.llm.complete(steps.SYSTEM, prompt, json_mode=True)
            try:
                p = steps.parse_json(raw)
                edits = p.get("edits", [])
                if not edits:
                    return ProposeOut(ok=False, reason="no_fix", proposal=p, error="The runbook does not cover this failure.")
                if not steps.cites_retrieved(p.get("runbook_section"), i.runbook):
                    raise PolicyViolation(f"fix does not cite a retrieved runbook section (cited={str(p.get('runbook_section'))[:80]!r}; "
                                          f"allowed={[h['section'][:80] for h in i.runbook]})")
                changes = steps.apply_edits(edits, read, self.s)
                break
            except (ValueError, KeyError, TypeError, PolicyViolation) as e:
                last_err = str(e)
                prompt += f"\n\nYour previous answer was REJECTED by the checker: {last_err}\nAnswer again with corrected JSON only."
        else:
            return ProposeOut(ok=False, reason="fix_rejected_by_policy", error=last_err)
        out = "\n".join([p.get("summary", "")] + [c.diff + c.new_content for c in changes])
        if (err := self._guard(out, "OUTPUT")):
            return ProposeOut(ok=False, reason="guardrail_blocked", error=err)
        raw_changes = [c.__dict__ for c in changes]
        return ProposeOut(ok=True, proposal={**p, "changes": raw_changes}, diff_hash=steps.diff_hash(raw_changes))

    @activity.defn
    @_classify
    def open_pr(self, i: PrIn) -> PrOut:
        p = i.proposal
        changes = [FileChange(**c) for c in p["changes"]]
        body = f"{redact(p['summary'])}\n\nRunbook: `{p['runbook_section']}`\nDiff hash: `{i.diff_hash}`\nAwaiting approval."
        pr = self.gh.open_fix_pr(i.repo, i.base, f"{self.s.branch_prefix}run-{i.run_id}", f"ci-fix: run {i.run_id}", body, changes)
        return PrOut(**pr.__dict__)

    @activity.defn
    @_classify
    def execute_fix(self, i: ExecIn) -> None:
        self.gh.merge_pr(i.repo, i.pr.number, i.pr.head_sha)  # pinned to the SHA that was approved
        self.gh.rerun(i.repo, i.run_id)

    @activity.defn
    @_classify
    def close_pr(self, i: ClosePrIn) -> None:
        self.gh.close_pr(i.repo, i.pr.number, i.pr.branch)

    def all(self):
        return [self.fetch_logs, self.analyze, self.retrieve_runbook, self.propose_fix, self.open_pr, self.execute_fix, self.close_pr]
