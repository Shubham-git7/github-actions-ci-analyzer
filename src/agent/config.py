"""Settings read from the environment. Only place that touches os.environ."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    llm_backend: str = field(default_factory=lambda: os.getenv("LLM_BACKEND", "bedrock"))
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


def get_settings() -> Settings:
    return Settings()
