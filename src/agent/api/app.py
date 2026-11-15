"""FastAPI app: AgentCore contract (/ping, /invocations on :8080) plus the run/approval API and UI."""
import pathlib
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent.audit import AuditLog
from agent.auth.rbac import AuthError, Principal
from agent.guard.policy import PolicyViolation, check_repo
from agent.runindex import MemoryRunIndex

STATIC = pathlib.Path(__file__).resolve().parents[3] / "ui"  # dev only: prod serves ui/ from CloudFront
STEPS = ["fetch_logs", "analyze", "retrieve_runbook", "propose_fix", "open_pr", "await_approval", "execute_fix"]


class TriggerBody(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    run_id: int
    base: str = "main"


class DecisionBody(BaseModel):
    approved: bool
    diff_hash: str


def create_app(graph, verifier: Callable[[str], Principal], settings, audit: AuditLog, index=None) -> FastAPI:
    app = FastAPI(title="CI Analyzer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    index = index or MemoryRunIndex()

    def known() -> list[str]:
        return index.list()

    @app.exception_handler(AuthError)
    def auth_error(_, e: AuthError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    def principal(authorization: str = Header(default="")) -> Principal:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            return verifier(authorization[7:])
        except AuthError as e:
            raise HTTPException(e.status, str(e)) from e

    def view(rid: str) -> dict:
        st = graph.get_state({"configurable": {"thread_id": rid}})
        v = dict(st.values)
        v.pop("logs", None)  # raw logs never leave the backend
        done = [n for n in STEPS if n in (st.metadata or {}).get("writes", {}) or False]
        return {"id": rid, "status": v.get("status"), "analysis": v.get("analysis"), "runbook": v.get("runbook"),
                "proposal": v.get("proposal"), "pr": v.get("pr"), "diff_hash": v.get("diff_hash"),
                "error": v.get("error"), "next": list(st.next), "repo": v.get("repo"),
                "triggered_by": v.get("triggered_by"), "steps": STEPS, "done": done}

    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    @app.get("/config.json")
    def public_config():
        return {"domain": settings.cognito_domain, "client_id": settings.cognito_client_id, "api_url": ""}

    @app.post("/runs")
    def trigger(body: TriggerBody, p: Principal = Depends(principal)):
        p.require("runs:trigger")
        try:
            check_repo(body.repo, settings)
        except PolicyViolation as e:
            raise HTTPException(400, str(e)) from e
        rid = uuid.uuid4().hex[:12]
        index.add(rid)
        audit.record(rid=rid, event="triggered", run=body.run_id, by=p.sub, repo=body.repo)
        graph.invoke({"rid": rid, "repo": body.repo, "run_id": body.run_id, "base": body.base, "triggered_by": p.sub},
                     {"configurable": {"thread_id": rid}})
        return view(rid)

    @app.get("/runs")
    def list_runs(p: Principal = Depends(principal)):
        p.require("runs:read")
        return [view(r) for r in known()]

    @app.get("/runs/{rid}")
    def get_run(rid: str, p: Principal = Depends(principal)):
        p.require("runs:read")
        if rid not in known():
            raise HTTPException(404)
        return view(rid)

    @app.post("/runs/{rid}/decision")
    def decide(rid: str, body: DecisionBody, p: Principal = Depends(principal)):
        p.require("runs:approve")
        if rid not in known():
            raise HTTPException(404)
        v = view(rid)
        if v["status"] != "awaiting_approval":
            raise HTTPException(409, "run is not awaiting approval")
        if p.sub == v["triggered_by"]:
            audit.record(rid=rid, event="self_approval_blocked", by=p.sub)
            raise HTTPException(403, "you cannot approve a run you triggered")
        graph.invoke(Command(resume={"approved": body.approved, "diff_hash": body.diff_hash,
                                     "approver_sub": p.sub, "permissions": ["runs:approve"]}),
                     {"configurable": {"thread_id": rid}})
        return view(rid)

    class Invocation(BaseModel):
        """AgentCore only proxies /invocations and /ping, so every action goes through here."""
        action: str = "trigger"
        repo: str | None = None
        run_id: int | None = None
        base: str = "main"
        id: str | None = None
        approved: bool | None = None
        diff_hash: str | None = None

    @app.post("/invocations")
    def invocations(body: Invocation, p: Principal = Depends(principal)):
        if body.action == "trigger":
            return trigger(TriggerBody(repo=body.repo or "", run_id=body.run_id or 0, base=body.base), p)
        if body.action == "list":
            return list_runs(p)
        if body.action == "get":
            return get_run(body.id or "", p)
        if body.action == "decision":
            return decide(body.id or "", DecisionBody(approved=bool(body.approved), diff_hash=body.diff_hash or ""), p)
        if body.action == "audit":
            p.require("runs:read")
            if (body.id or "") not in known():
                raise HTTPException(404)
            return audit.for_run(body.id)
        raise HTTPException(400, "unknown action")

    if STATIC.is_dir():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
