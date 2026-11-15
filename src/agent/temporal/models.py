"""Typed inputs/outputs that cross the workflow/activity boundary (and end up in workflow history).
Nothing here may carry a credential; logs are redacted before they are returned."""
from pydantic import BaseModel, Field

STEPS = ["fetch_logs", "analyze", "retrieve_runbook", "propose_fix", "open_pr", "await_approval", "execute_fix"]


class RunInput(BaseModel):
    rid: str
    repo: str
    run_id: int
    base: str = "main"
    triggered_by: str
    approval_ttl_s: int = 86_400


class LogsOut(BaseModel):
    logs: str = ""
    blocked: str = ""  # non-empty = stopped by the input guardrail


class ProposeIn(BaseModel):
    repo: str
    base: str
    run_id: int
    analysis: str
    runbook: list[dict]
    logs: str


class ProposeOut(BaseModel):
    ok: bool
    error: str = ""
    reason: str = ""  # audit event name when not ok: fix_rejected_by_policy | guardrail_blocked | no_fix
    proposal: dict | None = None
    diff_hash: str = ""


class PrIn(BaseModel):
    repo: str
    base: str
    run_id: int
    proposal: dict
    diff_hash: str


class PrOut(BaseModel):
    number: int
    url: str
    branch: str
    head_sha: str = ""


class ExecIn(BaseModel):
    repo: str
    run_id: int
    pr: PrOut


class ClosePrIn(BaseModel):
    repo: str
    pr: PrOut


class Decision(BaseModel):
    approved: bool
    diff_hash: str = ""
    approver_sub: str
    permissions: list[str] = Field(default_factory=list)


class DecisionResult(BaseModel):
    outcome: str  # approved | rejected | denied
    reason: str = ""


class RunView(BaseModel):
    id: str
    repo: str
    triggered_by: str
    status: str = "analyzing"
    analysis: str | None = None
    runbook: list[dict] | None = None
    proposal: dict | None = None
    pr: PrOut | None = None
    diff_hash: str | None = None
    error: str | None = None
    next: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=lambda: list(STEPS))
    done: list[str] = Field(default_factory=list)
    audit: list[dict] = Field(default_factory=list)
