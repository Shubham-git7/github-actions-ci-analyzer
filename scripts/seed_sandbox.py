"""Reset the PUBLIC sandbox repo to a deliberately broken CI state and find the failed run to analyze.

Needs a token that can write Contents, Pull requests, Actions and Workflows on DEMO_REPO (fine-grained PAT, that repo only).
It only touches DEMO_REPO: package.json, the workflow, README, and any open ci-fix/* PRs left from earlier demos.
Last line of stdout is DEMO_RUN_ID=<id>, which start_local.sh stores."""
import base64
import calendar
import os
import sys
import time

import httpx

repo = os.environ.get("DEMO_REPO") or sys.exit("DEMO_REPO not set")
tf = os.environ.get("GITHUB_TOKEN_FILE")
token = os.environ.get("GITHUB_TOKEN") or (open(tf).read().strip() if tf else "") or sys.exit("no GitHub token")
http = httpx.Client(base_url="https://api.github.com", timeout=30, headers={
    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

BROKEN_PACKAGE = '{\n  "name": "ci-sandbox",\n  "version": "1.0.0",\n  "scripts": {\n    "test": "echo ok"\n  }\n}\n'
WORKFLOW = """name: ci
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm test
      - run: npm run build
"""
README = ("# CI sandbox\n\nA deliberately broken repo for the TWS Pipeline Harness demo: `npm run build` fails because "
          "package.json has no build script. The harness reads the failure, finds the fix in its runbook, opens a PR, and "
          "waits for a human approver. Safe to delete.\n")


def get(path, **kw):
    return http.get(path, **kw)


def put_file(path: str, content: str, message: str) -> bool:
    cur = get(f"/repos/{repo}/contents/{path}", params={"ref": "main"})
    body = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": "main"}
    if cur.status_code == 200:
        if base64.b64decode(cur.json()["content"]).decode() == content:
            return False
        body["sha"] = cur.json()["sha"]
    r = http.put(f"/repos/{repo}/contents/{path}", json=body)
    if r.status_code in (403, 404) and path.startswith(".github/"):
        sys.exit("GitHub refused the workflow file: the token needs the 'Workflows: read and write' permission.")
    r.raise_for_status()
    return True


info = get(f"/repos/{repo}")
if info.status_code == 404:
    sys.exit(f"{repo} not found or not visible to the token. Create it first, e.g.: gh repo create {repo} --public")
info.raise_for_status()
if info.json().get("private"):
    print(f"note: {repo} is private. The demo is meant for a public sandbox repo, but it works either way.")
if not info.json().get("permissions", {}).get("push"):
    sys.exit("the token cannot push to this repo (needs Contents: read and write).")

# 1. close leftovers from earlier demos so the next run starts clean
for pr in get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50}).json():
    if pr["head"]["ref"].startswith("ci-fix/"):
        http.patch(f"/repos/{repo}/pulls/{pr['number']}", json={"state": "closed"})
        http.delete(f"/repos/{repo}/git/refs/heads/{pr['head']['ref']}")
        print(f"closed stale PR #{pr['number']}")

# 2. put the repo in its broken state
started = time.time()
changed = [put_file("README.md", README, "chore: sandbox readme"),
           put_file(".github/workflows/ci.yml", WORKFLOW, "ci: build workflow"),
           put_file("package.json", BROKEN_PACKAGE, "chore: reset sandbox to the broken state")]
print("committed changes" if any(changed) else "repo already in the broken state")

# 3. find (or trigger) a run that really failed in a job (a run rejected at parse time has no jobs and nothing to analyze)
def failed_in_a_job(run_id: int) -> bool:
    jobs = get(f"/repos/{repo}/actions/runs/{run_id}/jobs").json().get("jobs", [])
    return any(j["conclusion"] == "failure" for j in jobs)


deadline, dispatched = time.time() + 300, False
print("waiting for the failing workflow run...")
while time.time() < deadline:
    runs = get(f"/repos/{repo}/actions/runs", params={"branch": "main", "per_page": 10}).json().get("workflow_runs", [])
    fresh = [r for r in runs if calendar.timegm(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ")) >= started - 60]
    if fresh and all(r["status"] == "completed" for r in fresh):  # let every run triggered by our pushes finish first
        for r in sorted(fresh, key=lambda r: r["created_at"], reverse=True):
            if r["conclusion"] == "failure" and failed_in_a_job(r["id"]):
                print(f"failed run: {r['html_url']}")
                print(f"DEMO_RUN_ID={r['id']}")
                sys.exit(0)
    if not dispatched and time.time() - started > 25 and not fresh:
        http.post(f"/repos/{repo}/actions/workflows/ci.yml/dispatches", json={"ref": "main"})  # nothing started on push
        dispatched = True
        print("no run started on push; dispatched one")
    time.sleep(5)
sys.exit("timed out waiting for a failed run. Check the Actions tab of the repo.")
