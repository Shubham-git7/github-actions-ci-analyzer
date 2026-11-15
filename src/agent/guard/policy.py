"""Hard rules for what the agent may change. Violations raise; nothing is auto-corrected."""
from dataclasses import dataclass

from agent.config import Settings


class PolicyViolation(Exception):
    pass


@dataclass(frozen=True)
class FileChange:
    path: str
    new_content: str
    diff: str


def check_repo(repo: str, s: Settings) -> None:
    if repo not in s.repo_allowlist:
        raise PolicyViolation(f"repo {repo!r} is not in the allowlist")


def check_branch(branch: str, s: Settings) -> None:
    if not branch.startswith(s.branch_prefix):
        raise PolicyViolation(f"branch must start with {s.branch_prefix!r}, got {branch!r}")


def check_changes(changes: list[FileChange], s: Settings) -> None:
    if not changes:
        raise PolicyViolation("empty fix")
    total = 0
    for c in changes:
        if ".." in c.path.split("/") or c.path.startswith("/"):
            raise PolicyViolation(f"unsafe path {c.path!r}")
        if not any(c.path.startswith(p) or c.path == p for p in s.allowed_paths):
            raise PolicyViolation(f"path {c.path!r} is outside the allowed paths")
        total += len(c.diff.splitlines())
    if total > s.max_diff_lines:
        raise PolicyViolation(f"diff too large ({total} > {s.max_diff_lines} lines)")
