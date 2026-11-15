import base64
import hashlib
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from agent.auth.rbac import AuthError, OidcVerifier
from agent.devauth.app import build, create_app

REDIR = "http://localhost:8080/"
ISS = "http://localhost:9000"


def pkce():
    v = "v" * 50
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def c():
    return TestClient(create_app(ISS, "tws-ui", {REDIR}, ["http://localhost:8080"]), follow_redirects=False)


def login(c, persona, challenge):
    r = c.post("/oauth2/pick", data={"persona": persona, "redirect_uri": REDIR, "code_challenge": challenge, "state": "s"})
    return r.headers["location"].split("code=")[1].split("&")[0]


def verifier_for(c):
    v = OidcVerifier.__new__(OidcVerifier)
    v._issuer, v._client_id, v._groups_claim = ISS, "tws-ui", "cognito:groups"
    keys = c.get("/.well-known/jwks.json").json()["keys"]

    class J:
        def get_signing_key_from_jwt(self, t):
            return type("K", (), {"key": jwt.PyJWK(keys[0]).key})()
    v._jwks = J()
    return v


def test_full_pkce_flow_yields_token_the_api_verifier_accepts(c):
    verifier, challenge = pkce()
    assert c.get("/oauth2/authorize", params={"client_id": "tws-ui", "redirect_uri": REDIR, "code_challenge": challenge}).status_code == 200
    code = login(c, "bob", challenge)
    t = c.post("/oauth2/token", data={"grant_type": "authorization_code", "client_id": "tws-ui", "code": code, "redirect_uri": REDIR,
                                       "code_verifier": verifier}).json()
    p = verifier_for(c).verify(t["access_token"])
    assert p.sub == "bob" and p.can("runs:approve") and not p.can("runs:trigger")
    r = c.post("/oauth2/token", data={"grant_type": "refresh_token", "client_id": "tws-ui", "refresh_token": t["refresh_token"]})
    assert r.status_code == 200


def test_wrong_verifier_reused_code_and_bad_redirect_are_refused(c):
    verifier, challenge = pkce()
    code = login(c, "carol", challenge)
    bad = c.post("/oauth2/token", data={"grant_type": "authorization_code", "client_id": "tws-ui", "code": code,
                                        "redirect_uri": REDIR, "code_verifier": "x" * 50})
    assert bad.status_code == 400
    # a code is single use, even after a failed attempt
    again = c.post("/oauth2/token", data={"grant_type": "authorization_code", "client_id": "tws-ui", "code": code,
                                          "redirect_uri": REDIR, "code_verifier": verifier})
    assert again.status_code == 400
    assert c.get("/oauth2/authorize", params={"client_id": "tws-ui", "redirect_uri": "http://evil/", "code_challenge": challenge}).status_code == 400
    assert c.get("/oauth2/authorize", params={"client_id": "other", "redirect_uri": REDIR, "code_challenge": challenge}).status_code == 400


def test_token_signed_elsewhere_or_expired_is_rejected(c):
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {"iss": ISS, "sub": "bob", "client_id": "tws-ui", "token_use": "access", "exp": int(time.time()) + 60}
    with pytest.raises(AuthError):
        verifier_for(c).verify(jwt.encode(claims, other, algorithm="RS256", headers={"kid": "x"}))


def test_refuses_to_start_without_dev_auth_flag(monkeypatch):
    monkeypatch.delenv("DEV_AUTH", raising=False)
    with pytest.raises(SystemExit):
        build()
