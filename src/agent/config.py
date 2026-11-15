"""Settings read from the environment. Only place that touches os.environ."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    llm_backend: str = field(default_factory=lambda: os.getenv("LLM_BACKEND", "ollama"))
    model_id: str = field(default_factory=lambda: os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5"))
    region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-west-2"))
    repo_allowlist: tuple[str, ...] = field(default_factory=lambda: _csv("GITHUB_REPO_ALLOWLIST"))
    branch_prefix: str = "ci-fix/"
    max_diff_lines: int = 200
    max_removed_lines: int = 1
    allowed_paths: tuple[str, ...] = (".github/workflows/", "requirements", "package.json", "pyproject.toml")
    guardrail_id: str = field(default_factory=lambda: os.getenv("GUARDRAIL_ID", ""))
    guardrail_version: str = field(default_factory=lambda: os.getenv("GUARDRAIL_VERSION", ""))
    cognito_pool_id: str = field(default_factory=lambda: os.getenv("COGNITO_USER_POOL_ID", ""))
    cognito_domain: str = field(default_factory=lambda: os.getenv("COGNITO_DOMAIN", ""))
    cognito_client_id: str = field(default_factory=lambda: os.getenv("COGNITO_CLIENT_ID", ""))
    # --- local / Temporal profile ---
    llm_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    llm_model: str = field(default_factory=lambda: os.getenv("MODEL", "qwen2.5-coder:7b"))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "540")))
    temporal_address: str = field(default_factory=lambda: os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    temporal_namespace: str = field(default_factory=lambda: os.getenv("TEMPORAL_NAMESPACE", "default"))
    task_queue: str = field(default_factory=lambda: os.getenv("TASK_QUEUE", "ci-analyzer"))
    codec_key: str = field(default_factory=lambda: os.getenv("CODEC_KEY", ""))
    approval_ttl_s: int = field(default_factory=lambda: int(os.getenv("APPROVAL_TTL_S", "86400")))
    runbook_dir: str = field(default_factory=lambda: os.getenv("RUNBOOK_DIR", "runbooks"))
    demo_repo: str = field(default_factory=lambda: os.getenv("DEMO_REPO", ""))
    demo_run_id: str = field(default_factory=lambda: os.getenv("DEMO_RUN_ID", ""))
    # OIDC: issuer is what tokens carry; jwks_url is what the API can reach (differs inside docker)
    auth_issuer: str = field(default_factory=lambda: os.getenv("AUTH_ISSUER", ""))
    auth_jwks_url: str = field(default_factory=lambda: os.getenv("AUTH_JWKS_URL", ""))
    auth_client_id: str = field(default_factory=lambda: os.getenv("AUTH_CLIENT_ID", "tws-ui"))
    auth_public_url: str = field(default_factory=lambda: os.getenv("AUTH_PUBLIC_URL", ""))
    codec_server: bool = field(default_factory=lambda: os.getenv("CODEC_SERVER", "") == "1")


def get_settings() -> Settings:
    return Settings()
