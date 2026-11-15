"""Thin same-origin proxy: CloudFront /api/invocations -> AgentCore runtime /invocations.

It adds no authority. The Cognito JWT is forwarded untouched and verified by AgentCore (JWT authorizer)
and again by the app (RBAC). The proxy only narrows the surface: one path, POST only, known actions,
bounded body, validated session id, no other headers forwarded.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request

_URL = os.environ.get("RUNTIME_URL", "")  # tests / local override


def runtime_url() -> str:
    """The runtime ARN lives in SSM so the Lambda does not depend on the runtime at deploy time."""
    global _URL
    if not _URL:
        from urllib.parse import quote

        import boto3

        arn = boto3.client("ssm").get_parameter(Name=os.environ["RUNTIME_ARN_PARAM"])["Parameter"]["Value"]
        region = arn.split(":")[3]
        _URL = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    return _URL
ALLOWED_ACTIONS = {"trigger", "list", "get", "decision", "audit"}
MAX_BODY = 16 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{33,100}$")


def _resp(status: int, body: dict | str, extra: dict | None = None) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})},
            "body": body if isinstance(body, str) else json.dumps(body)}


def handler(event, _ctx):
    http = event["requestContext"]["http"]
    if http["method"] != "POST" or event.get("rawPath") != "/api/invocations":
        return _resp(404, {"message": "not found"})
    h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = h.get("authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        return _resp(401, {"message": "missing bearer token"})
    sid = h.get("x-amzn-bedrock-agentcore-runtime-session-id", "")
    if not SESSION_RE.match(sid):
        return _resp(400, {"message": "bad session id"})
    raw = event.get("body") or ""
    body = base64.b64decode(raw).decode() if event.get("isBase64Encoded") else raw
    if len(body) > MAX_BODY:
        return _resp(413, {"message": "body too large"})
    try:
        if json.loads(body).get("action") not in ALLOWED_ACTIONS:
            raise ValueError
    except (ValueError, AttributeError):
        return _resp(400, {"message": "unknown action"})
    req = urllib.request.Request(runtime_url(), data=body.encode(), method="POST", headers={
        "Authorization": auth, "Content-Type": "application/json", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid})
    try:
        with urllib.request.urlopen(req, timeout=110) as r:
            return _resp(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        return _resp(e.code, e.read().decode()[:2000])
    except Exception:  # network / timeout: do not leak internals
        return _resp(502, {"message": "upstream unavailable"})
