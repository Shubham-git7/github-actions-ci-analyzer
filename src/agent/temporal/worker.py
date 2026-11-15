"""Worker process: the only place that holds GITHUB_TOKEN and talks to the model.
Run: python -m agent.temporal.worker"""
import asyncio
import concurrent.futures
import logging
import os

from temporalio.worker import Worker

from agent.config import get_settings
from agent.github.client import RestGitHubClient
from agent.llm import get_llm
from agent.rag.retriever import BedrockKBRetriever, KeywordRetriever
from agent.temporal.activities import CiActivities
from agent.temporal.client import connect
from agent.temporal.workflows import FixRunWorkflow


def build_activities(s) -> CiActivities:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN_FILE") and open(os.environ["GITHUB_TOKEN_FILE"]).read().strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set. Run ./start_local.sh to configure it.")
    if not s.repo_allowlist:
        raise SystemExit("GITHUB_REPO_ALLOWLIST is empty: the agent refuses to touch any repo.")
    retriever = BedrockKBRetriever(os.environ["KNOWLEDGE_BASE_ID"], s.region) if os.getenv("KNOWLEDGE_BASE_ID") \
        else KeywordRetriever.from_dir(s.runbook_dir)
    guard = None
    if s.guardrail_id:  # AWS profile only; the local profile relies on redaction, policy and the human gate
        from agent.guard.bedrock import BedrockGuard

        guard = BedrockGuard(s.guardrail_id, s.guardrail_version, s.region)
    return CiActivities(get_llm(s), RestGitHubClient(token, s), retriever, s, guard)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    acts = build_activities(s)
    client = await connect(s)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        worker = Worker(client, task_queue=s.task_queue, workflows=[FixRunWorkflow], activities=acts.all(), activity_executor=pool)
        logging.info("worker up: queue=%s llm=%s repos=%s", s.task_queue, s.llm_backend, ",".join(s.repo_allowlist))
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
