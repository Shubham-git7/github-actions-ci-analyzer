"""Amazon Bedrock ApplyGuardrail wrapper. Fails closed: any error counts as blocked."""


class GuardrailBlocked(Exception):
    pass


class BedrockGuard:
    def __init__(self, guardrail_id: str, version: str, region: str, client=None):
        if not guardrail_id or not version or version == "DRAFT":
            raise ValueError("a numbered guardrail version is required (DRAFT is refused)")
        import boto3

        self._c = client or boto3.client("bedrock-runtime", region_name=region)
        self._id, self._v = guardrail_id, version

    def check(self, text: str, source: str = "INPUT") -> None:
        try:
            r = self._c.apply_guardrail(
                guardrailIdentifier=self._id, guardrailVersion=self._v, source=source,
                content=[{"text": {"text": text[:20_000]}}],
            )
        except Exception as e:  # fail closed
            raise GuardrailBlocked(f"safety check unavailable: {type(e).__name__}") from e
        if r.get("action") == "GUARDRAIL_INTERVENED":
            raise GuardrailBlocked("blocked by guardrail")
