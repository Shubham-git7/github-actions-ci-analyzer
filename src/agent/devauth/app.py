"""DEV-ONLY OIDC issuer for the local demo. Anyone who can reach it can become any persona.
It speaks just enough OAuth2 (authorization code + PKCE S256, refresh) and publishes a JWKS so the API verifies
tokens through the exact code path it uses for Cognito. Refuses to start unless DEV_AUTH=1.

Run: uvicorn agent.devauth.app:build --factory --port 9000"""
import base64
import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

PERSONAS = {
    "alice": ("Alice", "admin", "Can trigger analyses and approve other people's runs"),
    "bob": ("Bob", "approver", "Can approve or reject runs he did not trigger"),
    "carol": ("Carol", "viewer", "Read-only: sees everything, can approve nothing"),
}
TTL = 3600


def create_app(issuer: str, client_id: str, redirects: set[str], origins: list[str]) -> FastAPI:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = secrets.token_hex(4)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    codes: dict[str, dict] = {}
    refresh: dict[str, str] = {}
    cid = client_id
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET"], allow_headers=["*"])

    def mint(persona: str) -> str:
        name, group, _ = PERSONAS[persona]
        now = int(time.time())
        return jwt.encode({"iss": issuer, "sub": persona, "username": name, "cognito:groups": [group], "client_id": client_id,
                           "token_use": "access", "iat": now, "exp": now + TTL}, pem, algorithm="RS256", headers={"kid": kid})

    @app.get("/.well-known/jwks.json")
    def jwks():
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": kid, "use": "sig", "alg": "RS256"}
        return {"keys": [jwk]}

    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    @app.get("/oauth2/authorize", response_class=HTMLResponse)
    def authorize(client_id: str, redirect_uri: str, code_challenge: str, state: str = "", response_type: str = "code",
                  code_challenge_method: str = "S256"):
        if client_id != cid or redirect_uri not in redirects or response_type != "code" or code_challenge_method != "S256":
            raise HTTPException(400, "invalid authorization request")
        q = {"redirect_uri": redirect_uri, "code_challenge": code_challenge, "state": state}
        cards = "".join(
            f'<form method="post" action="/oauth2/pick"><input type="hidden" name="persona" value="{p}">'
            + "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in q.items())
            + f'<button><b>{n}</b><span class="g">{g}</span><small>{html.escape(d)}</small></button></form>'
            for p, (n, g, d) in PERSONAS.items())
        return HTMLResponse(PAGE.replace("{{cards}}", cards))

    @app.post("/oauth2/pick")
    def pick(persona: str = Form(), redirect_uri: str = Form(), code_challenge: str = Form(), state: str = Form("")):
        if persona not in PERSONAS or redirect_uri not in redirects:
            raise HTTPException(400, "invalid request")
        code = secrets.token_urlsafe(24)
        codes[code] = {"persona": persona, "challenge": code_challenge, "redirect": redirect_uri, "exp": time.time() + 120}
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=303)

    @app.post("/oauth2/token")
    def token(grant_type: str = Form(), client_id: str = Form(), code: str = Form(""), redirect_uri: str = Form(""),
              code_verifier: str = Form(""), refresh_token: str = Form("")):
        if client_id != cid:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if grant_type == "authorization_code":
            c = codes.pop(code, None)  # single use
            digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if not c or c["exp"] < time.time() or c["redirect"] != redirect_uri or not secrets.compare_digest(digest, c["challenge"]):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            rt = secrets.token_urlsafe(32)
            refresh[rt] = c["persona"]
            return {"access_token": mint(c["persona"]), "refresh_token": rt, "token_type": "Bearer", "expires_in": TTL}
        if grant_type == "refresh_token" and refresh_token in refresh:
            return {"access_token": mint(refresh[refresh_token]), "token_type": "Bearer", "expires_in": TTL}
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    return app


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Dev sign-in · TWS Pipeline Harness</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0B0E13;color:#E9EDF4;font:16px system-ui,sans-serif}
main{width:min(440px,92vw)}h1{font-size:22px;margin:0 0 4px}p{color:#8793A6;margin:0 0 20px;font-size:14px}
.warn{background:#2a1d08;border:1px solid #FFB638;color:#FFB638;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:18px}
form{margin:0 0 10px}button{all:unset;box-sizing:border-box;width:100%;cursor:pointer;display:grid;gap:2px;padding:14px 16px;border-radius:12px;background:#151A24;border:1px solid #2B3446}
button:hover,button:focus-visible{border-color:#FFB638}b{font-size:17px}.g{color:#FFB638;font:600 12px ui-monospace,monospace;text-transform:uppercase}small{color:#8793A6}</style>
<main><h1>Choose a persona</h1><p>Local demo identity provider.</p>
<div class=warn>Dev auth only. This page grants any role to anyone who can reach it. Never expose it.</div>{{cards}}</main>"""


def build() -> FastAPI:
    if os.getenv("DEV_AUTH") != "1":
        raise SystemExit("devauth refuses to start without DEV_AUTH=1 (it is a demo identity provider, not for real use)")
    base = os.environ.get("AUTH_ISSUER", "http://localhost:9000")
    app_url = os.environ.get("APP_URL", "http://localhost:8080")
    return create_app(base, os.environ.get("AUTH_CLIENT_ID", "tws-ui"), {app_url + "/"}, [app_url])

