"""Production wiring. Run: uvicorn agent.api.main:app --host 0.0.0.0 --port 8080"""
import os

import boto3

from agent.api.app import create_app
from agent.audit import AuditLog
from agent.auth.rbac import CognitoVerifier
from agent.config import get_settings
from agent.github.client import RestGitHubClient
from agent.graph import build_graph
from agent.guard.bedrock import BedrockGuard
from agent.llm import get_llm
from agent.rag.retriever import BedrockKBRetriever

s = get_settings()
secret_id = os.environ["GITHUB_TOKEN_SECRET_ID"]
token = boto3.client("secretsmanager", region_name=s.region).get_secret_value(SecretId=secret_id)["SecretString"]
verifier = CognitoVerifier(s.region, s.cognito_pool_id, s.cognito_client_id)
audit = AuditLog()  # replaced below when a shared table is configured
table = os.getenv("STATE_TABLE")
if table:  # shared durable state: any session / any approver sees every run
    from langgraph_checkpoint_aws import DynamoDBSaver

    from agent.audit import DynamoAuditLog
    from agent.runindex import DynamoRunIndex

    cp, index = DynamoDBSaver(table, region_name=s.region, consistent_read=True), DynamoRunIndex(table, s.region)
    audit = DynamoAuditLog(table, s.region)
else:  # local dev only
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from agent.runindex import MemoryRunIndex

    cp, index = SqliteSaver(sqlite3.connect(os.getenv("STATE_DB", "state.db"), check_same_thread=False)), MemoryRunIndex()
guard = BedrockGuard(s.guardrail_id, s.guardrail_version, s.region)  # required in prod; refuses DRAFT
graph = build_graph(get_llm(s), RestGitHubClient(token, s),
                    BedrockKBRetriever(os.environ["KNOWLEDGE_BASE_ID"], s.region), s, audit,
                    checkpointer=cp, guard=guard)
app = create_app(graph, verifier.verify, s, audit, index)
