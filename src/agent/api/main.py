"""Wiring. Run: uvicorn agent.api.main:app --host 0.0.0.0 --port 8080

The api never imports the GitHub client or the model: it can only start workflows and send updates."""
from agent.api.app import create_app
from agent.auth.rbac import CognitoVerifier, OidcVerifier
from agent.config import get_settings
from agent.temporal.client import connect

s = get_settings()
if s.auth_issuer:  # local profile: dev OIDC issuer
    verifier = OidcVerifier(s.auth_issuer, s.auth_jwks_url, s.auth_client_id)
else:  # AWS profile
    verifier = CognitoVerifier(s.region, s.cognito_pool_id, s.cognito_client_id)

app = create_app(lambda: connect(s), verifier.verify, s)
