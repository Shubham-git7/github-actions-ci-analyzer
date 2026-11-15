import json

import httpx
import pytest

from agent import steps
from agent.config import Settings
from agent.llm import OllamaLLM, ScriptedLLM, get_llm
from agent.temporal.codec import EncryptionCodec
from tests.helpers import KEY, LOG, ORIGINAL


def test_ollama_adapter_requests_json_mode_and_zero_temperature():
    seen = {}

    def handler(r):
        seen.update(json.loads(r.content))
        return httpx.Response(200, json={"message": {"content": '{"a": 1}'}})

    llm = OllamaLLM("http://o", "m", client=httpx.Client(base_url="http://o", transport=httpx.MockTransport(handler)))
    assert llm.complete("s", "u", json_mode=True) == '{"a": 1}'
    assert seen["format"] == "json" and seen["options"]["temperature"] == 0 and seen["stream"] is False
    seen.clear()
    llm.complete("s", "u")
    assert "format" not in seen


def test_ollama_http_errors_propagate_so_temporal_can_retry():
    llm = OllamaLLM("http://o", "m", client=httpx.Client(base_url="http://o", transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete("s", "u")


def test_scripted_model_solves_only_the_known_failure_and_passes_the_policy_pipeline():
    llm = ScriptedLLM()
    analysis = llm.complete(steps.SUMMARIZE, LOG)
    runbook = [{"section": "ci.md#Missing build script", "text": "## Missing build script\nadd it"}]
    prompt = steps.build_prompt(analysis, runbook, {"package.json": ORIGINAL}, LOG)
    p = steps.parse_json(llm.complete(steps.SYSTEM, prompt, json_mode=True))
    assert steps.cites_retrieved(p["runbook_section"], runbook)
    ch = steps.apply_edits(p["edits"], lambda path: ORIGINAL, Settings(repo_allowlist=("a/b",)))
    assert '+    "build": "echo build ok"' in ch[0].diff
    other = steps.parse_json(llm.complete(steps.SYSTEM, "ERROR: something else", json_mode=True))
    assert other["edits"] == []


def test_backend_selection():
    assert isinstance(get_llm(Settings(llm_backend="scripted")), ScriptedLLM)
    assert isinstance(get_llm(Settings(llm_backend="ollama")), OllamaLLM)
    with pytest.raises(ValueError):
        get_llm(Settings(llm_backend="nope"))


async def test_codec_round_trip_and_wrong_key_fails():
    from cryptography.fernet import Fernet, InvalidToken
    from temporalio.api.common.v1 import Payload

    p = Payload(metadata={"encoding": b"json/plain"}, data=b'"hello"')
    enc = await EncryptionCodec(KEY).encode([p])
    assert enc[0].data != p.data and b"hello" not in enc[0].data
    assert (await EncryptionCodec(KEY).decode(enc))[0].data == b'"hello"'
    with pytest.raises(InvalidToken):
        await EncryptionCodec(Fernet.generate_key()).decode(enc)


def test_retriever_matches_a_long_prose_analysis_with_punctuation():
    from agent.rag.retriever import KeywordRetriever

    r = KeywordRetriever.from_dir("runbooks")
    prose = ("The root cause of this CI failure is that the `build` script is missing in the project's `package.json`. "
             "The error message indicates that npm cannot find the \"build\" script, so the step fails. To fix it, add a build script.")
    assert "Missing build script" in r.retrieve(prose)[0].section


def test_bracketed_citation_is_normalized_but_still_verified():
    rb = [{"section": "ci-fixes.md#Missing build script", "text": "## Missing build script\nadd it"}]
    assert steps.cites_retrieved("[ci-fixes.md#Missing build script]", rb)
    assert not steps.cites_retrieved("[ci-fixes.md#Invented]", rb) and not steps.cites_retrieved("[]", rb)
