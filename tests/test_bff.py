import base64
import importlib.util
import json
import os
import pathlib
import urllib.error

os.environ["RUNTIME_URL"] = "https://runtime.example/invocations"
spec = importlib.util.spec_from_file_location("bff", pathlib.Path(__file__).parents[1] / "infra/bff/handler.py")
bff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bff)
SID = "x" * 40


def ev(body='{"action":"list"}', method="POST", path="/api/invocations", auth="Bearer t", sid=SID, b64=False):
    h = {k: v for k, v in {"Authorization": auth, "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid}.items() if v}
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": h,
            "body": base64.b64encode(body.encode()).decode() if b64 else body, "isBase64Encoded": b64}


class Resp:
    status = 200
    def read(self): return b"[]"
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_rejects_bad_requests():
    assert bff.handler(ev(method="GET"), None)["statusCode"] == 404
    assert bff.handler(ev(path="/api/other"), None)["statusCode"] == 404
    assert bff.handler(ev(auth=""), None)["statusCode"] == 401
    assert bff.handler(ev(sid="short"), None)["statusCode"] == 400
    assert bff.handler(ev(body='{"action":"rm -rf"}'), None)["statusCode"] == 400
    assert bff.handler(ev(body="not json"), None)["statusCode"] == 400
    assert bff.handler(ev(body='{"action":"list","x":"' + "a" * 20000 + '"}'), None)["statusCode"] == 413


def test_forwards_only_safe_headers_and_body(monkeypatch):
    seen = {}
    def fake(req, timeout): seen.update(h=dict(req.header_items()), data=req.data, url=req.full_url); return Resp()
    monkeypatch.setattr(bff.urllib.request, "urlopen", fake)
    out = bff.handler(ev(b64=True), None)
    assert out["statusCode"] == 200 and out["body"] == "[]"
    assert seen["url"] == "https://runtime.example/invocations" and json.loads(seen["data"]) == {"action": "list"}
    assert set(k.lower() for k in seen["h"]) == {"authorization", "content-type", "x-amzn-bedrock-agentcore-runtime-session-id"}


def test_upstream_errors_pass_status_but_not_internals(monkeypatch):
    def deny(req, timeout): raise urllib.error.HTTPError("u", 403, "x", {}, __import__("io").BytesIO(b'{"message":"Access denied."}'))
    monkeypatch.setattr(bff.urllib.request, "urlopen", deny)
    assert bff.handler(ev(), None)["statusCode"] == 403
    def boom(req, timeout): raise OSError("secret internal host 10.0.0.5")
    monkeypatch.setattr(bff.urllib.request, "urlopen", boom)
    r = bff.handler(ev(), None)
    assert r["statusCode"] == 502 and "10.0.0" not in r["body"]
