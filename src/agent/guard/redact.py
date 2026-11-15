"""Remove secrets from text before it reaches the LLM, a PR body, the UI or logs."""
import math
import re

PLACEHOLDER = "[REDACTED]"

_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),            # classic GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),           # fine-grained PAT
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),              # AWS access key id
    re.compile(r"(?i)\baws_secret_access_key\s*[=:]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*['\"]?[^\s'\"]{6,}"),
    re.compile(r"https://[^\s:@/]+:[^\s@/]+@"),                # creds in URLs
]
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")


def _entropy(s: str) -> float:
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def redact(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub(PLACEHOLDER, text)
    return _HIGH_ENTROPY.sub(lambda m: PLACEHOLDER if _entropy(m.group()) > 4.0 else m.group(), text)
