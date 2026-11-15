"""The model is a replaceable part: one tiny interface, three adapters.

    complete(system, user, json_mode=False) -> str

Adapters never retry: Temporal owns retries (activity retry policy), so a model hiccup is
visible in the workflow history instead of being swallowed in a client library."""
import json
import re
from typing import Protocol

import httpx

from agent.config import Settings, get_settings


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str: ...


class OllamaLLM:
    """Local models through Ollama's /api/chat. json_mode constrains decoding to valid JSON, which small models need."""

    def __init__(self, url: str, model: str, timeout_s: float = 540, client: httpx.Client | None = None):
        self._http = client or httpx.Client(base_url=url, timeout=timeout_s)
        self._model = model

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        body = {"model": self._model, "stream": False, "options": {"temperature": 0, "num_ctx": 8192},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        r = self._http.post("/api/chat", json=body)
        r.raise_for_status()
        return r.json()["message"]["content"]


class BedrockLLM:
    """AWS profile: Bedrock Converse. Not exercised by the local demo."""

    def __init__(self, model_id: str, region: str, client=None):
        import boto3

        self._c = client or boto3.client("bedrock-runtime", region_name=region)
        self._model = model_id

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        r = self._c.converse(modelId=self._model, system=[{"text": system}],
                             messages=[{"role": "user", "content": [{"text": user}]}],
                             inferenceConfig={"temperature": 0, "maxTokens": 2000})
        return "".join(p.get("text", "") for p in r["output"]["message"]["content"])


class ScriptedLLM:
    """Deterministic stand-in for machines that cannot run a model (`start_local.sh --fast`) and for tests.
    It solves exactly one class of failure (npm 'missing script: X'); anything else yields no edits, so the harness
    routes it to a human. It is labeled as scripted in the UI so nobody mistakes it for a real model."""

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        if not json_mode:
            m = re.search(r"missing script:?\s*[\"']?(\w[\w:-]*)", user, re.I)
            if m:
                return f"npm run {m[1]} fails: package.json has no {m[1]} script (missing script {m[1]})."
            return "The log shows a CI step failing; no known pattern was recognised."
        m = re.search(r"missing script:?\s*[\"']?(\w[\w:-]*)", user, re.I)
        src = re.search(r"^\[([^\]\n]+)\]", user, re.M)
        test = re.search(r'"test":\s*"[^"\n]*"', user)
        if not (m and src and test):
            return json.dumps({"summary": "No scripted fix for this failure.", "edits": []})
        name = m[1]
        return json.dumps({
            "summary": f"Add the missing {name} script to package.json.", "runbook_section": src[1],
            "edits": [{"path": "package.json", "find": test[0],
                       "replace": f'{test[0]},\n    "{name}": "echo {name} ok"'}]})


def get_llm(settings: Settings | None = None) -> LLM:
    s = settings or get_settings()
    if s.llm_backend == "ollama":
        return OllamaLLM(s.llm_url, s.llm_model, s.llm_timeout_s)
    if s.llm_backend == "scripted":
        return ScriptedLLM()
    if s.llm_backend == "bedrock":
        return BedrockLLM(s.model_id, s.region)
    raise ValueError(f"unknown LLM_BACKEND {s.llm_backend!r} (ollama | scripted | bedrock)")
