"""Real end-to-end run: failed GitHub Actions run -> PR -> human approval -> merge + rerun.

Usage: uv run python scripts/e2e_demo.py OWNER/REPO RUN_ID [--scripted] [--approve|--reject]
First call opens the PR and stops. Second call (same args + --approve) merges and re-runs.
Needs GITHUB_TOKEN (fine-grained PAT) in .env. Default LLM is Bedrock (billable, small);
--scripted uses a fixed fix for the demo repo's missing npm build script instead.
You are asked to type APPROVE; the approval goes through the same graph rules as the API.
"""
import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agent.audit import AuditLog
from agent.config import Settings
from agent.github.client import RestGitHubClient
from agent.graph import build_graph
from agent.rag.retriever import KeywordRetriever

repo, run_id = sys.argv[1], int(sys.argv[2])
token = os.environ.get("GITHUB_TOKEN") or sys.exit("GITHUB_TOKEN missing (.env)")
settings = Settings(repo_allowlist=(repo,))
gh = RestGitHubClient(token, settings)
retriever = KeywordRetriever.from_dir("runbooks")


class Scripted:
    def invoke(self, messages):
        if "Summarize" in str(messages[0]):
            return SimpleNamespace(content="npm run build fails: package.json has no build script.")
        return SimpleNamespace(content=json.dumps({
            "summary": "Add the missing build script.", "runbook_section": next(iter(retriever._sections)),
            "edits": [{"path": "package.json", "find": '"test": "echo ok"',
                       "replace": '"test": "echo ok",\n    "build": "echo build ok"'}]}))


if "--scripted" in sys.argv:
    llm = Scripted()
else:
    from agent.llm import get_llm
    llm = get_llm(settings)

audit = AuditLog("e2e-audit.jsonl")
cp = SqliteSaver(sqlite3.connect('e2e-state.db', check_same_thread=False))
g = build_graph(llm, gh, retriever, settings, audit, checkpointer=cp)
cfg = {"configurable": {"thread_id": f"e2e-{run_id}"}}
phase2 = "--approve" in sys.argv or "--reject" in sys.argv
if not phase2:
    g.invoke({"repo": repo, "run_id": run_id, "triggered_by": "alice-trigger", "base": "main"}, cfg)
st = g.get_state(cfg).values
print("status:", st.get("status"), "| error:", st.get("error"))
if st.get("status") != "awaiting_approval":
    sys.exit("not awaiting approval (see status/error above)")
print("PR:", st["pr"]["url"], "\nrunbook:", st["proposal"]["runbook_section"], "\ndiff hash:", st["diff_hash"])
if "--reject" in sys.argv:
    g.invoke(Command(resume={"approved": False, "approver_sub": "bob-approver", "permissions": ["runs:approve"],
                             "diff_hash": st["diff_hash"]}), cfg)
    sys.exit("rejected; nothing merged")
if "--approve" not in sys.argv:
    sys.exit("PR opened. Review it, then rerun with --approve (or --reject).")
out = g.invoke(Command(resume={"approved": True, "approver_sub": "bob-approver", "permissions": ["runs:approve"],
                               "diff_hash": st["diff_hash"]}), cfg)
print("final status:", out["status"])
time.sleep(5)
for _ in range(30):
    runs = gh._http.get(f"/repos/{repo}/actions/runs", params={"branch": "main", "per_page": 1}).json()["workflow_runs"]
    if runs and runs[0]["status"] == "completed":
        print("latest main run:", runs[0]["conclusion"], runs[0]["html_url"])
        break
    time.sleep(6)
