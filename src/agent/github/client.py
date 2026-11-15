"""The only module that holds the GitHub token. Everything else uses the GitHubClient protocol."""
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from agent.config import Settings
from agent.guard.policy import FileChange, check_branch, check_repo


@dataclass
class PR:
    number: int
    url: str
    branch: str
    head_sha: str = ""


class GitHubClient(Protocol):
    def failed_run_logs(self, repo: str, run_id: int) -> str: ...
    def open_fix_pr(self, repo: str, base: str, branch: str, title: str, body: str, changes: list[FileChange]) -> PR: ...
    def file_content(self, repo: str, path: str, ref: str) -> str | None: ...
    def merge_pr(self, repo: str, number: int, expected_sha: str) -> None: ...
    def rerun(self, repo: str, run_id: int) -> None: ...


class RestGitHubClient:
    """Fine-grained PAT, repo allowlist and branch prefix enforced on every call."""

    def __init__(self, token: str, settings: Settings):
        self._s = settings
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
            follow_redirects=True,
        )

    def failed_run_logs(self, repo: str, run_id: int) -> str:
        check_repo(repo, self._s)
        jobs = self._http.get(f"/repos/{repo}/actions/runs/{run_id}/jobs").raise_for_status().json()["jobs"]
        out = []
        for j in jobs:
            if j["conclusion"] == "failure":
                out.append(self._http.get(f"/repos/{repo}/actions/jobs/{j['id']}/logs").raise_for_status().text)
        return "\n".join(out)[-60_000:]

    def file_content(self, repo: str, path: str, ref: str) -> str | None:
        import base64

        check_repo(repo, self._s)
        r = self._http.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        return base64.b64decode(r.raise_for_status().json()["content"]).decode()

    def open_fix_pr(self, repo, base, branch, title, body, changes) -> PR:
        import base64

        check_repo(repo, self._s)
        check_branch(branch, self._s)
        sha = self._http.get(f"/repos/{repo}/git/ref/heads/{base}").raise_for_status().json()["object"]["sha"]
        self._http.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}).raise_for_status()
        head = sha
        for c in changes:
            existing = self._http.get(f"/repos/{repo}/contents/{c.path}", params={"ref": branch})
            payload = {
                "message": f"ci-fix: update {c.path}",
                "content": base64.b64encode(c.new_content.encode()).decode(),
                "branch": branch,
            }
            if existing.status_code == 200:
                payload["sha"] = existing.json()["sha"]
            r = self._http.put(f"/repos/{repo}/contents/{c.path}", json=payload).raise_for_status().json()
            head = r["commit"]["sha"]
        pr = self._http.post(
            f"/repos/{repo}/pulls", json={"title": title, "body": body, "head": branch, "base": base}
        ).raise_for_status().json()
        return PR(pr["number"], pr["html_url"], branch, head)

    def merge_pr(self, repo: str, number: int, expected_sha: str) -> None:
        check_repo(repo, self._s)
        self._http.put(
            f"/repos/{repo}/pulls/{number}/merge", json={"sha": expected_sha, "merge_method": "squash"}
        ).raise_for_status()

    def rerun(self, repo: str, run_id: int) -> None:
        check_repo(repo, self._s)
        self._http.post(f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs").raise_for_status()


@dataclass
class FakeGitHubClient:
    logs: str = ""
    files: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def failed_run_logs(self, repo, run_id):
        self.calls.append(("logs", repo, run_id))
        return self.logs

    def file_content(self, repo, path, ref):
        return self.files.get(path)

    def open_fix_pr(self, repo, base, branch, title, body, changes):
        self.calls.append(("pr", repo, branch))
        return PR(1, f"https://github.com/{repo}/pull/1", branch, "sha-abc")

    def merge_pr(self, repo, number, expected_sha):
        self.calls.append(("merge", repo, number, expected_sha))

    def rerun(self, repo, run_id):
        self.calls.append(("rerun", repo, run_id))
