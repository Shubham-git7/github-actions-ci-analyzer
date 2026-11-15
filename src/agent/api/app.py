"""FastAPI front door: authenticates, authorizes, then talks to Temporal. It holds NO GitHub token and runs no agent code.

Contract (AgentCore-compatible): GET /ping, POST /invocations {action: trigger|list|get|decision|audit}.
All durable state lives in Temporal; the api can restart, scale or die without losing a run."""
import asyncio
import os
import pathlib
from collections.abc import Awaitable, Callable
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import SearchAttributePair, TypedSearchAttributes, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from agent.auth.rbac import AuthError, Principal
from agent.guard.policy import PolicyViolation, check_repo
from agent.temporal.models import Decision, DecisionResult, RunInput, RunView
from agent.temporal.workflows import CI_REPO, CI_STATUS, CI_TRIGGERED_BY, FixRunWorkflow

STATIC = pathlib.Path(os.getenv("UI_DIR") or pathlib.Path(__file__).resolve().parents[3] / "ui")
QUERY_TIMEOUT = timedelta(seconds=4)  # a dead worker must not hang the run list
LIST_LIMIT = 50


class TriggerBody(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    run_id: int
    base: str = "main"


class DecisionBody(BaseModel):
    approved: bool
    diff_hash: str


def workflow_id(repo: str, run_id: int) -> str:
    return f"fix-{repo.replace('/', '-')}-{run_id}"


def create_app(client: Client | Callable[[], Awaitable[Client]], verifier: Callable[[str], Principal], settings) -> FastAPI:
    """`client` is a connected Temporal client, or an async factory (connect lazily so /ping works while Temporal boots)."""
    _c = client if isinstance(client, Client) else None

    async def tc() -> Client:
        nonlocal _c
        if _c is None:
            _c = await client()
        return _c

    app = FastAPI(title="TWS Pipeline Harness", docs_url=None, redoc_url=None, openapi_url=None)
    csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           f"font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' {settings.auth_public_url}; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/codec"):
            resp.headers["Content-Security-Policy"] = csp
        return resp

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

    # ---- helpers -----------------------------------------------------------------------
    async def view_of(rid: str) -> dict:
        try:
            v = await (await tc()).get_workflow_handle(rid).query(FixRunWorkflow.view, rpc_timeout=QUERY_TIMEOUT)
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "unknown run") from e
            return await stale_view(rid)
        except Exception:  # worker down / query timed out: report what visibility knows instead of failing
            return await stale_view(rid)
        return public(v)

    def public(v: RunView, stale: bool = False) -> dict:
        d = v.model_dump(exclude={"audit"})
        d["stale"] = stale
        return d

    async def stale_view(rid: str) -> dict:
        ex = await (await tc()).get_workflow_handle(rid).describe()
        sa = ex.typed_search_attributes
        return public(RunView(id=rid, repo=sa.get(CI_REPO) or "", triggered_by=sa.get(CI_TRIGGERED_BY) or "",
                              status=sa.get(CI_STATUS) or "analyzing"), stale=True)

    # ---- actions -----------------------------------------------------------------------
    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    @app.get("/config.json")
    def public_config():
        return {"domain": settings.cognito_domain,
                "client_id": settings.auth_client_id if settings.auth_public_url else settings.cognito_client_id,
                "auth_base": settings.auth_public_url, "api_url": "",
                "dev_auth": bool(settings.auth_public_url),
                "llm": {"backend": settings.llm_backend, "model": settings.llm_model if settings.llm_backend == "ollama" else ""},
                "demo": {"repo": settings.demo_repo, "run_id": settings.demo_run_id}}

    async def trigger(body: TriggerBody, p: Principal) -> dict:
        p.require("runs:trigger")
        try:
            check_repo(body.repo, settings)
        except PolicyViolation as e:
            raise HTTPException(400, str(e)) from e
        rid = workflow_id(body.repo, body.run_id)
        try:
            await (await tc()).start_workflow(
                FixRunWorkflow.run,
                RunInput(rid=rid, repo=body.repo, run_id=body.run_id, base=body.base, triggered_by=p.sub,
                         approval_ttl_s=settings.approval_ttl_s),
                id=rid, task_queue=settings.task_queue, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                search_attributes=TypedSearchAttributes([SearchAttributePair(CI_REPO, body.repo),
                                                         SearchAttributePair(CI_TRIGGERED_BY, p.sub)]))
        except WorkflowAlreadyStartedError as e:
            raise HTTPException(409, "this run is already being processed") from e
        # Return immediately: the workflow runs on its own and the UI polls. Nothing here waits on the agent.
        return public(RunView(id=rid, repo=body.repo, triggered_by=p.sub, next=["fetch_logs"]))

    async def list_runs(p: Principal) -> list[dict]:
        p.require("runs:read")
        ids = list(dict.fromkeys([w.id async for w in (await tc()).list_workflows('WorkflowType = "FixRunWorkflow"', limit=LIST_LIMIT)]))  # a re-triggered run shares its id
        return list(await asyncio.gather(*(view_of(i) for i in ids)))

    async def get_run(rid: str, p: Principal) -> dict:
        p.require("runs:read")
        return await view_of(rid)

    async def decide(rid: str, body: DecisionBody, p: Principal) -> dict:
        p.require("runs:approve")
        d = Decision(approved=body.approved, diff_hash=body.diff_hash, approver_sub=p.sub, permissions=["runs:approve"])
        handle = (await tc()).get_workflow_handle(rid)
        try:
            res: DecisionResult = await asyncio.wait_for(handle.execute_update(FixRunWorkflow.decide, d), 20)
        except WorkflowUpdateFailedError as e:
            msg = e.cause.message if isinstance(e.cause, ApplicationError) else "update rejected"
            raise HTTPException(409, msg) from e
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND and "completed" not in e.message:
                raise HTTPException(404, "unknown run") from e
            raise HTTPException(409, "run is not awaiting approval") from e  # already finished
        except TimeoutError as e:
            raise HTTPException(503, "worker unreachable: the decision was not confirmed; check the run before retrying") from e
        if res.outcome == "denied":
            raise HTTPException(403, res.reason)
        return await view_of(rid)

    class Invocation(BaseModel):
        """AgentCore only proxies /invocations and /ping, so every action goes through here."""
        action: str = "list"
        repo: str | None = None
        run_id: int | None = None
        base: str = "main"
        id: str | None = None
        approved: bool | None = None
        diff_hash: str | None = None

    @app.post("/invocations")
    async def invocations(body: Invocation, p: Principal = Depends(principal)):
        if body.action == "trigger":
            return await trigger(TriggerBody(repo=body.repo or "", run_id=body.run_id or 0, base=body.base), p)
        if body.action == "list":
            return await list_runs(p)
        if body.action == "get":
            return await get_run(body.id or "", p)
        if body.action == "decision":
            return await decide(body.id or "", DecisionBody(approved=bool(body.approved), diff_hash=body.diff_hash or ""), p)
        if body.action == "audit":
            p.require("runs:read")
            try:
                v = await (await tc()).get_workflow_handle(body.id or "").query(FixRunWorkflow.view, rpc_timeout=QUERY_TIMEOUT)
            except RPCError as e:
                raise HTTPException(404 if e.status == RPCStatusCode.NOT_FOUND else 503, "audit trail unavailable") from e
            return v.audit
        raise HTTPException(400, "unknown action")

    if settings.codec_server and settings.codec_key:  # local only: lets the Temporal UI decode encrypted payloads
        from fastapi.middleware.cors import CORSMiddleware
        from google.protobuf import json_format
        from temporalio.api.common.v1 import Payloads

        from agent.temporal.codec import EncryptionCodec

        codec = EncryptionCodec(settings.codec_key)
        app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8233", "http://127.0.0.1:8233"],
                           allow_methods=["POST", "OPTIONS"], allow_headers=["*"])

        async def transform(req: Request, fn):
            pl = json_format.ParseDict(await req.json(), Payloads())
            return json_format.MessageToDict(Payloads(payloads=await fn(pl.payloads)))

        @app.post("/codec/decode")
        async def codec_decode(req: Request):
            return await transform(req, codec.decode)

        @app.post("/codec/encode")
        async def codec_encode(req: Request):
            return await transform(req, codec.encode)

    if STATIC.is_dir():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
