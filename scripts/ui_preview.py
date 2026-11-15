"""Local UI preview with fake LLM/GitHub and a stub login. No AWS, no GitHub. Not for production.
Usage: uv run python scripts/ui_preview.py  ->  http://localhost:8765
Switch user in the browser console: sessionStorage.setItem('tok','viewer'|'admin'|'approver'); location.reload()
"""
import pathlib
import shutil
import tempfile

import uvicorn
from tests.test_graph import FILES, LOG, PROPOSAL, RUNBOOK, FakeLLM

import agent.api.app as appmod
from agent.audit import AuditLog
from agent.auth.rbac import AuthError, Principal
from agent.config import Settings
from agent.github.client import FakeGitHubClient
from agent.graph import build_graph
from agent.runindex import MemoryRunIndex

tmp = pathlib.Path(tempfile.mkdtemp())
shutil.copytree(appmod.STATIC, tmp / "s")
js = (tmp / "s" / "app.js").read_text()
js = js.replace("const c=parseJwt(t);", 'const c={approver:{sub:"bob","cognito:groups":["approver"]},viewer:{sub:"carol","cognito:groups":["viewer"]},admin:{sub:"alice","cognito:groups":["admin"]}}[t]||parseJwt(t);')
js = js.replace('const t=store.get("tok");', 'const t=store.get("tok")||"approver";')
(tmp / "s" / "app.js").write_text(js)
appmod.STATIC = tmp / "s"

S = Settings(repo_allowlist=("acme/app",))
USERS = {"admin": Principal("alice", "alice", frozenset({"admin"})),
         "approver": Principal("bob", "bob", frozenset({"approver"})),
         "viewer": Principal("carol", "carol", frozenset({"viewer"}))}


def verifier(t):
    if t not in USERS:
        raise AuthError("bad token")
    return USERS[t]


audit = AuditLog()
idx = MemoryRunIndex()
g = build_graph(FakeLLM(PROPOSAL), FakeGitHubClient(logs=LOG, files=FILES), RUNBOOK, S, audit)
app = appmod.create_app(g, verifier, S, audit, idx)
for rid, user in [("a1b2c3d4e5f6", "alice"), ("f6e5d4c3b2a1", "alice")]:
    idx.add(rid)
    g.invoke({"rid": rid, "repo": "acme/app", "run_id": 7, "triggered_by": user}, {"configurable": {"thread_id": rid}})
uvicorn.run(app, port=8765, log_level="warning")
