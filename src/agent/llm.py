"""Single swap point for the model (Bedrock in prod, any LangChain chat model in tests)."""
from agent.config import Settings, get_settings


def get_llm(settings: Settings | None = None):
    s = settings or get_settings()
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(model=s.model_id, region_name=s.region, temperature=0, max_tokens=2000)
