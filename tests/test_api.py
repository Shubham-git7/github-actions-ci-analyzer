import pytest
from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.audit import AuditLog
from agent.auth.rbac import AuthError, Principal
from agent.config import Settings
from agent.github.client import FakeGitHubClient
from agent.graph import build_graph
from tests.test_graph import FILES, LOG, PROPOSAL, RUNBOOK, FakeLLM

S = Settings(repo_allowlist=("acme/app",))
USERS = {"admin": Principal("alice", "alice", frozenset({"admin"})),
         "approver": Principal("bob", "bob", frozenset({"approver"})),
         "viewer": Principal("carol", "carol", frozenset({"viewer"}))}


def verifier(tok):
    if tok not in USERS:
        raise AuthError("bad token")
    return USERS[tok]


@pytest.fixture
def client():
    gh = FakeGitHubClient(logs=LOG, files=FILES)
    audit = AuditLog()
    g = build_graph(FakeLLM(PROPOSAL), gh, RUNBOOK, S, audit)
    c = TestClient(create_app(g, verifier, S, audit))
    c.gh = gh
    return c


def h(t):
    return {"Authorization": f"Bearer {t}"}


def test_ping_and_ui_public(client):
    assert client.get("/ping").json() == {"status": "Healthy"}
    assert client.get("/").status_code == 200


def test_auth_required(client):
    assert client.get("/runs").status_code == 401
    assert client.get("/runs", headers=h("nope")).status_code == 401


def test_full_flow_and_rbac(client):
    body = {"repo": "acme/app", "run_id": 7}
    assert client.post("/runs", json=body, headers=h("viewer")).status_code == 403
    assert client.post("/runs", json=body | {"repo": "evil/x"}, headers=h("admin")).status_code == 400
    r = client.post("/runs", json=body, headers=h("admin")).json()
    assert r["status"] == "awaiting_approval" and "logs" not in r
    d = {"approved": True, "diff_hash": r["diff_hash"]}
    path = f"/runs/{r['id']}/decision"
    assert client.post(path, json=d, headers=h("viewer")).status_code == 403
    assert client.post(path, json=d, headers=h("admin")).status_code == 403  # admin triggered it, and lacks approve? self-approval
    assert not any(c[0] == "merge" for c in client.gh.calls)
    assert client.post(path, json=d, headers=h("approver")).json()["status"] == "executed"
    assert client.post(path, json=d, headers=h("approver")).status_code == 409


def test_invocations_carries_every_action_with_same_rules(client):
    inv = lambda tok, **b: client.post("/invocations", json=b, headers=h(tok))
    assert inv("viewer", action="trigger", repo="acme/app", run_id=7).status_code == 403
    r = inv("admin", action="trigger", repo="acme/app", run_id=7).json()
    assert inv("viewer", action="list").status_code == 200
    d = dict(action="decision", id=r["id"], approved=True, diff_hash=r["diff_hash"])
    assert inv("viewer", **d).status_code == 403
    assert inv("admin", **d).status_code == 403  # self-approval
    assert inv("approver", **d).json()["status"] == "executed"
    assert inv("approver", action="bogus").status_code == 400


def test_second_app_instance_with_shared_state_sees_and_approves_the_run():
    """Models two AgentCore sessions: alice triggers in one, bob approves in another."""
    from langgraph.checkpoint.memory import MemorySaver

    from agent.runindex import MemoryRunIndex

    cp, idx, gh, audit = MemorySaver(), MemoryRunIndex(), FakeGitHubClient(logs=LOG, files=FILES), AuditLog()
    mk = lambda: TestClient(create_app(build_graph(FakeLLM(PROPOSAL), gh, RUNBOOK, S, audit, checkpointer=cp), verifier, S, audit, idx))
    s1, s2 = mk(), mk()
    r = s1.post("/invocations", json={"action": "trigger", "repo": "acme/app", "run_id": 7}, headers=h("admin")).json()
    assert [x["id"] for x in s2.post("/invocations", json={"action": "list"}, headers=h("approver")).json()] == [r["id"]]
    d = {"action": "decision", "id": r["id"], "approved": True, "diff_hash": r["diff_hash"]}
    assert s2.post("/invocations", json=d, headers=h("approver")).json()["status"] == "executed"


def test_audit_trail_per_run_and_headers(client):
    r = client.post("/invocations", json={"action": "trigger", "repo": "acme/app", "run_id": 7}, headers=h("admin")).json()
    d = {"action": "decision", "id": r["id"], "approved": True, "diff_hash": r["diff_hash"]}
    client.post("/invocations", json=d, headers=h("admin"))      # blocked: self-approval
    client.post("/invocations", json=d, headers=h("approver"))
    ev = client.post("/invocations", json={"action": "audit", "id": r["id"]}, headers=h("viewer")).json()
    assert [e["event"] for e in ev] == ["triggered", "pr_opened", "self_approval_blocked", "decision", "fix_executed"]
    assert client.post("/invocations", json={"action": "audit", "id": "nope"}, headers=h("viewer")).status_code == 404
    resp = client.get("/ping")
    assert resp.headers["x-content-type-options"] == "nosniff" and resp.headers["cache-control"] == "no-store"
    assert client.get("/docs").status_code == 404


def test_dynamo_audit_sink_writes_and_reads():
    from agent.audit import DynamoAuditLog

    class Fake:
        def __init__(self): self.items = []
        def put_item(self, TableName, Item): self.items.append(Item)
        def query(self, **kw): return {"Items": [i for i in self.items if i["PK"]["S"] == kw["ExpressionAttributeValues"][":p"]["S"]]}

    out, fake = [], Fake()
    log = DynamoAuditLog("t", "us-west-2", client=fake, stream=out.append)
    log.record(rid="r1", event="a")
    log.record(rid="r2", event="b")
    assert [e["event"] for e in log.for_run("r1")] == ["a"] and len(out) == 2 and '"audit"' in out[0]
