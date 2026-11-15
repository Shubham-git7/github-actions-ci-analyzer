"""The HTTP front door against a real in-process Temporal server (fake GitHub, scripted model)."""
import concurrent.futures
import shutil
import uuid
from dataclasses import replace

import httpx
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent.api.app import create_app
from agent.auth.rbac import AuthError, Principal
from agent.llm import ScriptedLLM
from agent.temporal.activities import CiActivities
from agent.temporal.codec import data_converter
from agent.temporal.workflows import SEARCH_ATTRIBUTES, FixRunWorkflow
from tests.helpers import KEY, RUNBOOK, S, new_github

USERS = {"admin": Principal("alice", "alice", frozenset({"admin"})),
         "approver": Principal("bob", "bob", frozenset({"approver"})),
         "viewer": Principal("carol", "carol", frozenset({"viewer"}))}


def verifier(tok):
    if tok not in USERS:
        raise AuthError("bad token")
    return USERS[tok]


def h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
async def api():
    gh = new_github()
    cli = shutil.which("temporal")
    async with await WorkflowEnvironment.start_local(
            data_converter=data_converter(KEY), search_attributes=SEARCH_ATTRIBUTES,
            **({"dev_server_existing_path": cli} if cli else {})) as env:
        queue = uuid.uuid4().hex
        settings = replace(S, task_queue=queue, codec_key=KEY, codec_server=True, auth_public_url="http://auth.test")
        acts = CiActivities(ScriptedLLM(), gh, RUNBOOK, settings)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            async with Worker(env.client, task_queue=queue, workflows=[FixRunWorkflow], activities=acts.all(), activity_executor=pool):
                app = create_app(env.client, verifier, settings)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                    c.gh = gh
                    yield c


async def inv(c, tok, **body):
    return await c.post("/invocations", json=body, headers=h(tok))


async def wait_for(c, rid, status):
    import asyncio

    for _ in range(200):
        r = (await inv(c, "viewer", action="get", id=rid)).json()
        if r["status"] == status:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(r)


async def test_ping_and_public_config_and_headers(api):
    assert (await api.get("/ping")).json() == {"status": "Healthy"}
    cfg = (await api.get("/config.json")).json()
    assert cfg["dev_auth"] is True and cfg["auth_base"] == "http://auth.test"
    r = await api.get("/ping")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


async def test_auth_required(api):
    assert (await api.post("/invocations", json={"action": "list"})).status_code == 401
    assert (await inv(api, "nope", action="list")).status_code == 401


async def test_full_flow_rbac_and_separation_of_duties(api):
    body = {"action": "trigger", "repo": "acme/app", "run_id": 7}
    assert (await api.post("/invocations", json=body, headers=h("viewer"))).status_code == 403
    assert (await api.post("/invocations", json=body | {"repo": "evil/x"}, headers=h("admin"))).status_code == 400
    r = (await api.post("/invocations", json=body, headers=h("admin"))).json()
    assert r["status"] == "analyzing" and "logs" not in r  # returns at once; the agent runs on its own
    assert (await api.post("/invocations", json=body, headers=h("admin"))).status_code == 409  # no duplicate runs
    run = await wait_for(api, r["id"], "awaiting_approval")
    d = {"action": "decision", "id": r["id"], "approved": True, "diff_hash": run["diff_hash"]}
    assert (await api.post("/invocations", json=d, headers=h("viewer"))).status_code == 403
    assert (await api.post("/invocations", json=d, headers=h("admin"))).status_code == 403  # triggerer cannot approve
    assert (await api.post("/invocations", json=d | {"diff_hash": "bad"}, headers=h("approver"))).status_code == 403
    assert not any(c[0] == "merge" for c in api.gh.calls)
    assert (await api.post("/invocations", json=d, headers=h("approver"))).json()["status"] in ("approved", "executed")
    await wait_for(api, r["id"], "executed")
    assert (await api.post("/invocations", json=d, headers=h("approver"))).status_code == 409
    audit = (await inv(api, "viewer", action="audit", id=r["id"])).json()
    assert {"self_approval_blocked", "tampered_diff_blocked", "decision", "fix_executed"} <= {e["event"] for e in audit}


async def test_list_and_unknown_run(api):
    r = (await api.post("/invocations", json={"action": "trigger", "repo": "acme/app", "run_id": 8}, headers=h("admin"))).json()
    await wait_for(api, r["id"], "awaiting_approval")
    runs = (await inv(api, "viewer", action="list")).json()
    assert [x["id"] for x in runs] == [r["id"]] and runs[0]["status"] == "awaiting_approval"
    assert (await inv(api, "viewer", action="get", id="nope")).status_code == 404
    assert (await inv(api, "bogus", action="list")).status_code == 401
    assert (await inv(api, "viewer", action="wat")).status_code == 400


async def test_codec_endpoint_decodes_history_for_the_temporal_ui(api):
    from google.protobuf import json_format
    from temporalio.api.common.v1 import Payload, Payloads

    from agent.temporal.codec import EncryptionCodec

    enc = await EncryptionCodec(KEY).encode([Payload(metadata={"encoding": b"json/plain"}, data=b'"secret"')])
    body = json_format.MessageToDict(Payloads(payloads=enc))
    out = (await api.post("/codec/decode", json=body)).json()
    assert json_format.ParseDict(out, Payloads()).payloads[0].data == b'"secret"'
