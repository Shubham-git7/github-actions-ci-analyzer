"""Hard rules for what the agent may change. Violations raise; nothing is auto-corrected."""
import re
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


# Lines a CI *fix* has no business adding. A model that obeys an instruction hidden in a log (prompt injection) tends to
# add exactly this kind of thing; the runbook-citation check does not stop it, so this sensor looks at the added lines.
_SUSPICIOUS = [
    (re.compile(r"\b(curl|wget)\b"), "downloads from the network"),
    (re.compile(r"\|\s*(sh|bash|zsh)\b"), "pipes into a shell"),
    (re.compile(r"\beval\b"), "uses eval"),
    (re.compile(r"base64\s+(-d|--decode)"), "decodes a payload"),
    (re.compile(r"/dev/tcp/|\bnc\s+-"), "opens a network connection"),
    (re.compile(r"\"(pre|post)?install\"\s*:"), "adds an install lifecycle script"),
]


def check_suspicious(changes: list["FileChange"]) -> None:
    for c in changes:
        for ln in c.diff.splitlines():
            if ln.startswith("+") and not ln.startswith("+++"):
                for rx, why in _SUSPICIOUS:
                    if rx.search(ln):
                        raise PolicyViolation(f"suspicious added line in {c.path} ({why}): {ln[1:].strip()[:80]!r}")


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
    check_suspicious(changes)
