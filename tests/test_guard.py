import pytest

from agent.config import Settings
from agent.guard.policy import FileChange, PolicyViolation, check_branch, check_changes, check_repo
from agent.guard.redact import redact

S = Settings(repo_allowlist=("acme/app",))


@pytest.mark.parametrize("secret", [
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "github_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "password=hunter2hunter2",
    "https://user:s3cretpass@github.com/x",
    "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
])
def test_redacts(secret):
    out = redact(f"step failed\n{secret}\nmore")
    assert "[REDACTED]" in out
    assert "hunter2hunter2" not in out and "s3cretpass" not in out and "EXAMPLEKEY" not in out
    assert "AKIAIOSFODNN7" not in out and "a1B2c3D4" not in out


def test_plain_log_untouched():
    assert redact("ModuleNotFoundError: No module named 'requests'") == "ModuleNotFoundError: No module named 'requests'"


def test_repo_and_branch_rules():
    check_repo("acme/app", S)
    with pytest.raises(PolicyViolation):
        check_repo("evil/app", S)
    check_branch("ci-fix/run-1", S)
    with pytest.raises(PolicyViolation):
        check_branch("main", S)


def test_change_rules():
    ok = FileChange(".github/workflows/ci.yml", "x", "+a\n-b")
    check_changes([ok], S)
    for bad in [FileChange("src/app.py", "x", "+a"), FileChange(".github/workflows/../../etc/x", "x", "+a"),
                FileChange(".github/workflows/ci.yml", "x", "\n".join("+l" for _ in range(500)))]:
        with pytest.raises(PolicyViolation):
            check_changes([bad], S)
    with pytest.raises(PolicyViolation):
        check_changes([], S)
