"""One workflow per failed CI run. This is the durable core: if every process dies, the run resumes where it was.

Timeline:  fetch_logs -> analyze -> retrieve_runbook -> propose_fix -> open_pr -> [wait for human] -> execute_fix
A reject, a timeout or a failure after the PR exists runs the compensation (close PR, delete branch).

Workflow code is deterministic: no I/O, no clocks, no randomness. All of that lives in activities."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributePair, TypedSearchAttributes
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from agent.temporal.activities import CiActivities
    from agent.temporal.models import (
        ClosePrIn,
        Decision,
        DecisionResult,
        ExecIn,
        PrIn,
        ProposeIn,
        ProposeOut,
        PrOut,
        RunInput,
        RunView,
    )

CI_STATUS = SearchAttributeKey.for_keyword("CiStatus")
CI_REPO = SearchAttributeKey.for_keyword("CiRepo")
CI_TRIGGERED_BY = SearchAttributeKey.for_keyword("CiTriggeredBy")
SEARCH_ATTRIBUTES = [CI_STATUS, CI_REPO, CI_TRIGGERED_BY]  # must be registered on the server (compose does it)

GITHUB = dict(start_to_close_timeout=timedelta(seconds=60),
              retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                                       maximum_interval=timedelta(seconds=30), maximum_attempts=6))
# Local models on CPU are slow; the timeout is generous and the attempts few. LLM client retries are off, Temporal owns them.
LLM = dict(start_to_close_timeout=timedelta(minutes=10),
           retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=1), maximum_attempts=3))
LOCAL = dict(start_to_close_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=3))


@workflow.defn
class FixRunWorkflow:
    def __init__(self) -> None:
        self.v: RunView | None = None
        self._decision: Decision | None = None

    # ---- state helpers (deterministic) -------------------------------------------------
    def _event(self, event: str, **fields) -> None:
        e = {"ts": round(workflow.now().timestamp(), 3), "rid": self.v.id, "event": event, **fields}
        self.v.audit.append(e)
        workflow.logger.info("audit %s", e)  # replay-safe structured line

    def _status(self, status: str, error: str | None = None) -> None:
        self.v.status = status
        if error is not None:
            self.v.error = error
        workflow.upsert_search_attributes(TypedSearchAttributes([SearchAttributePair(CI_STATUS, status)]))

    def _done(self, step: str, next_: str | None = None) -> None:
        self.v.done.append(step)
        self.v.next = [next_] if next_ else []

    def _stop(self, reason: str, error: str, event: str | None = None) -> RunView:
        self._event(event or reason, reason=error)
        self._status("needs_human", error)
        return self.v

    # ---- the run -------------------------------------------------------------------------
    @workflow.run
    async def run(self, inp: RunInput) -> RunView:
        self.v = RunView(id=inp.rid, repo=inp.repo, triggered_by=inp.triggered_by, next=["fetch_logs"])
        self._event("triggered", run=inp.run_id, by=inp.triggered_by, repo=inp.repo)
        pr: PrOut | None = None
        try:
            logs = await workflow.execute_activity_method(CiActivities.fetch_logs, args=[inp.repo, inp.run_id], **GITHUB)
            if logs.blocked:
                return self._stop("guardrail_blocked", logs.blocked)
            self._done("fetch_logs", "analyze")

            self.v.analysis = await workflow.execute_activity_method(CiActivities.analyze, logs.logs, **LLM)
            self._done("analyze", "retrieve_runbook")

            self.v.runbook = await workflow.execute_activity_method(CiActivities.retrieve_runbook, args=[self.v.analysis, logs.logs], **LOCAL)
            if not self.v.runbook:
                return self._stop("no_runbook", "No runbook section matched this failure.")
            self._done("retrieve_runbook", "propose_fix")

            prop: ProposeOut = await workflow.execute_activity_method(
                CiActivities.propose_fix,
                ProposeIn(repo=inp.repo, base=inp.base, run_id=inp.run_id, analysis=self.v.analysis, runbook=self.v.runbook,
                          logs=logs.logs), **LLM)
            self.v.proposal = prop.proposal
            if not prop.ok:
                return self._stop(prop.reason, prop.error)
            self.v.diff_hash = prop.diff_hash
            self._done("propose_fix", "open_pr")

            pr = await workflow.execute_activity_method(
                CiActivities.open_pr,
                PrIn(repo=inp.repo, base=inp.base, run_id=inp.run_id, proposal=prop.proposal, diff_hash=prop.diff_hash), **GITHUB)
            self.v.pr = pr
            self._event("pr_opened", run=inp.run_id, pr=pr.number)
            self._done("open_pr", "await_approval")
            self._status("awaiting_approval")

            # ---- the human gate: durable, survives restarts, has a deadline ----
            try:
                await workflow.wait_condition(lambda: self._decision is not None, timeout=timedelta(seconds=inp.approval_ttl_s))
            except TimeoutError:
                await self._compensate(inp, pr)
                return self._stop("approval_timeout", f"No decision within {inp.approval_ttl_s}s. PR closed.")
            self._done("await_approval", "execute_fix")

            if self.v.status == "rejected":
                await self._compensate(inp, pr)
                return self.v

            await workflow.execute_activity_method(CiActivities.execute_fix, ExecIn(repo=inp.repo, run_id=inp.run_id, pr=pr), **GITHUB)
            self._event("fix_executed", run=inp.run_id)
            self._done("execute_fix")
            self._status("executed")
            return self.v
        except ActivityError as e:
            cause = e.cause.message if isinstance(e.cause, ApplicationError) else str(e.cause or e)
            if pr is not None and self.v.status != "executed":
                await self._compensate(inp, pr)
            return self._stop("step_failed", f"{e.activity_type} failed: {cause}")
        finally:
            await workflow.wait_condition(workflow.all_handlers_finished)

    async def _compensate(self, inp: RunInput, pr: PrOut) -> None:
        try:
            await workflow.execute_activity_method(CiActivities.close_pr, ClosePrIn(repo=inp.repo, pr=pr), **GITHUB)
            self._event("pr_closed", pr=pr.number)
        except ActivityError as e:  # best effort; the run result is still recorded
            self._event("pr_close_failed", pr=pr.number, reason=str(e.cause or e)[:200])

    # ---- interactive handlers ------------------------------------------------------------
    @workflow.update
    def decide(self, d: Decision) -> DecisionResult:
        """Authoritative gate. A bad attempt is recorded and refused; the run keeps waiting for a valid approver."""
        v = self.v
        if "runs:approve" not in d.permissions:
            return self._deny(d, "missing permission runs:approve")
        if d.approved:
            if d.approver_sub == v.triggered_by:
                return self._deny(d, "you cannot approve a run you triggered", event="self_approval_blocked")
            if d.diff_hash != v.diff_hash:
                return self._deny(d, "diff hash does not match the proposed change", event="tampered_diff_blocked")
        self._decision = d
        self._status("approved" if d.approved else "rejected")
        self._event("decision", approved=d.approved, approver=d.approver_sub)
        return DecisionResult(outcome="approved" if d.approved else "rejected")

    @decide.validator
    def _validate_decide(self, d: Decision) -> None:
        if self.v is None or self.v.status != "awaiting_approval":
            raise ValueError("run is not awaiting approval")

    def _deny(self, d: Decision, reason: str, event: str = "decision_denied") -> DecisionResult:
        self._event(event, by=d.approver_sub, reason=reason)
        return DecisionResult(outcome="denied", reason=reason)

    @workflow.query
    def view(self) -> RunView:
        return self.v or RunView(id="", repo="", triggered_by="")
