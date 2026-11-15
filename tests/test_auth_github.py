import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent.auth.rbac import AuthError, CognitoVerifier, Forbidden, Principal
from agent.config import Settings
from agent.github.client import RestGitHubClient
from agent.guard.policy import FileChange, PolicyViolation
from agent.runindex import DynamoRunIndex

POOL, CLIENT, REGION = "us-west-2_abc", "client1", "us-west-2"
ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_verifier():
    v = CognitoVerifier.__new__(CognitoVerifier)
    v._issuer, v._client_id = ISS, CLIENT

    class J:
        def get_signing_key_from_jwt(self, _t):
            return type("K", (), {"key": KEY.public_key()})()
    v._jwks = J()
    return v


def token(key=KEY, **over):
    claims = {"iss": ISS, "sub": "u-1", "username": "u-1", "token_use": "access", "client_id": CLIENT,
              "exp": int(time.time()) + 600, "cognito:groups": ["approver"], **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def test_valid_token_yields_principal_with_groups():
    p = make_verifier().verify(token())
    assert p.sub == "u-1" and p.can("runs:approve") and not p.can("runs:trigger")


@pytest.mark.parametrize("bad", [
    dict(iss="https://evil.example/pool"), dict(client_id="other-client"), dict(token_use="id"),
    dict(exp=int(time.time()) - 5), dict(iss=None),
])
def test_bad_claims_rejected(bad):
    with pytest.raises(AuthError):
        make_verifier().verify(token(**bad))


def test_wrong_signature_and_garbage_rejected():
    with pytest.raises(AuthError):
        make_verifier().verify(token(key=OTHER))
    with pytest.raises(AuthError):
        make_verifier().verify("not.a.jwt")


def test_role_permissions():
    viewer, approver, admin, none = (Principal("s", "s", frozenset(g)) for g in ({"viewer"}, {"approver"}, {"admin"}, set()))
    assert viewer.can("runs:read") and not viewer.can("runs:approve")
    assert approver.can("runs:approve") and not approver.can("runs:trigger")
    assert admin.can("runs:trigger") and not none.can("runs:read")
    with pytest.raises(Forbidden):
        viewer.require("runs:approve")


S = Settings(repo_allowlist=("acme/app",))


def gh_client(handler):
    c = RestGitHubClient("tok", S)
    c._http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return c


def test_github_client_enforces_policy_before_any_network_call():
    called = []
    c = gh_client(lambda r: called.append(r) or httpx.Response(200, json={}))
    with pytest.raises(PolicyViolation):
        c.failed_run_logs("evil/repo", 1)
    with pytest.raises(PolicyViolation):
        c.open_fix_pr("acme/app", "main", "main", "t", "b", [FileChange("package.json", "x", "+x")])
    with pytest.raises(PolicyViolation):
        c.merge_pr("evil/repo", 1, "sha")
    with pytest.raises(PolicyViolation):
        c.rerun("evil/repo", 1)
    assert called == []


def test_merge_is_pinned_to_the_approved_sha_and_pr_flow():
    seen = []

    def handler(r):
        seen.append((r.method, r.url.path, json.loads(r.content) if r.content else None))
        if r.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if "/contents/" in r.url.path and r.method == "GET":
            return httpx.Response(404, json={})
        if "/contents/" in r.url.path:
            return httpx.Response(200, json={"commit": {"sha": "head1"}})
        if r.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 9, "html_url": "https://github.com/acme/app/pull/9"})
        return httpx.Response(200, json={})

    c = gh_client(handler)
    pr = c.open_fix_pr("acme/app", "main", "ci-fix/run-1", "t", "b", [FileChange("package.json", "{}", "+x")])
    assert (pr.number, pr.head_sha, pr.branch) == (9, "head1", "ci-fix/run-1")
    c.merge_pr("acme/app", 9, "head1")
    merge = [s for s in seen if s[1].endswith("/pulls/9/merge")][0]
    assert merge[2] == {"sha": "head1", "merge_method": "squash"}


def test_dynamo_run_index_pages_and_sorts():
    class C:
        def __init__(self): self.items, self.n = [], 0
        def put_item(self, TableName, Item): self.items.append(Item)
        def query(self, **kw):
            self.n += 1
            if self.n == 1:
                return {"Items": [{"SK": {"S": "b"}}], "LastEvaluatedKey": {"x": 1}}
            return {"Items": [{"SK": {"S": "a"}}]}

    idx = DynamoRunIndex("t", "us-west-2", client=C())
    idx.add("a")
    assert idx.list() == ["a", "b"]
