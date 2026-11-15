"""Shared fixtures for workflow/API tests: fake GitHub with fault injection, scripted model, in-process Temporal."""
import concurrent.futures
import shutil
import uuid
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet
from temporalio.common import SearchAttributePair, TypedSearchAttributes
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent.config import Settings
from agent.github.client import FakeGitHubClient
from agent.llm import ScriptedLLM
from agent.rag.retriever import KeywordRetriever
from agent.temporal.activities import CiActivities
from agent.temporal.codec import data_converter
from agent.temporal.models import Decision, RunInput
from agent.temporal.workflows import CI_REPO, CI_TRIGGERED_BY, SEARCH_ATTRIBUTES, FixRunWorkflow

S = Settings(repo_allowlist=("acme/app",), llm_backend="scripted")
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
LOG = f"npm ERR! missing script: build\ncheckout token {TOKEN}\n"
RUNBOOK = KeywordRetriever({"ci.md#Missing build script": "When npm reports missing script build, add a build script to package.json"})
ORIGINAL = '{\n  "name": "x",\n  "scripts": {\n    "test": "echo ok"\n  }\n}\n'
KEY = Fernet.generate_key().decode()


class FlakyGitHub(FakeGitHubClient):
    """Raises a transient 502 for the first `fail_open` open_fix_pr calls."""

    def __init__(self, *a, fail_open: int = 0, **kw):
        super().__init__(*a, **kw)
        self.fail_open = fail_open

    def open_fix_pr(self, *a, **kw):
        self.calls.append(("pr-attempt",))
        if self.fail_open > 0:
            self.fail_open -= 1
            raise httpx.HTTPStatusError("bad gateway", request=httpx.Request("PUT", "http://gh/x"), response=httpx.Response(502))
        return super().open_fix_pr(*a, **kw)


def new_github(**kw) -> FakeGitHubClient:
    return FlakyGitHub(logs=LOG, files={"package.json": ORIGINAL}, **kw)


class Guard:
    def __init__(self, block_on=None):
        self.block_on = block_on

    def check(self, text, source="INPUT"):
        from agent.guard.bedrock import GuardrailBlocked

        if source == self.block_on:
            raise GuardrailBlocked("blocked by guardrail")


@asynccontextmanager
async def harness(gh=None, llm=None, retriever=RUNBOOK, guard=None):
    """In-process Temporal server + worker running the real workflow and activities against fakes."""
    gh = gh or new_github()
    acts = CiActivities(llm or ScriptedLLM(), gh, retriever, S, guard)
    cli = shutil.which("temporal")  # reuse a local CLI if present, otherwise the SDK downloads one
    async with await WorkflowEnvironment.start_local(
        data_converter=data_converter(KEY), search_attributes=SEARCH_ATTRIBUTES,
        **({"dev_server_existing_path": cli} if cli else {})) as env:
        queue = uuid.uuid4().hex
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            async with Worker(env.client, task_queue=queue, workflows=[FixRunWorkflow], activities=acts.all(), activity_executor=pool):
                h = Harness(env, queue, gh)
                yield h


class Harness:
    def __init__(self, env, queue, gh):
        self.env, self.queue, self.gh = env, queue, gh

    async def start(self, ttl=86_400, triggered_by="alice", run_id=7):
        return await self.env.client.start_workflow(
            FixRunWorkflow.run, RunInput(rid=uuid.uuid4().hex[:8], repo="acme/app", run_id=run_id, triggered_by=triggered_by,
                                         approval_ttl_s=ttl),
            id=uuid.uuid4().hex, task_queue=self.queue,
            search_attributes=TypedSearchAttributes([SearchAttributePair(CI_REPO, "acme/app"),
                                                     SearchAttributePair(CI_TRIGGERED_BY, triggered_by)]))

    async def until(self, h, status, tries=200):
        import asyncio

        for _ in range(tries):
            v = await h.query(FixRunWorkflow.view)
            if v.status == status:
                return v
            await asyncio.sleep(0.05)
        raise AssertionError(f"never reached {status}: {v.status} {v.error}")

    async def decide(self, h, **kw):
        v = await h.query(FixRunWorkflow.view)
        d = Decision(**({"approved": True, "approver_sub": "bob", "permissions": ["runs:approve"], "diff_hash": v.diff_hash} | kw))
        return await h.execute_update(FixRunWorkflow.decide, d)
