"""Cognito JWT verification and group-based permissions."""
from dataclasses import dataclass

import jwt

PERMISSIONS = {
    "viewer": {"runs:read"},
    "approver": {"runs:read", "runs:approve"},
    "admin": {"runs:read", "runs:approve", "runs:trigger"},
}


class AuthError(Exception):
    status = 401


class Forbidden(AuthError):
    status = 403


@dataclass(frozen=True)
class Principal:
    sub: str
    username: str
    groups: frozenset[str]

    def can(self, perm: str) -> bool:
        return any(perm in PERMISSIONS.get(g, set()) for g in self.groups)

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise Forbidden(f"missing permission {perm}")


class OidcVerifier:
    """Verifies an OIDC access token: RS256 signature from the issuer's JWKS, issuer, expiry, client and token type.
    The same code path serves Cognito (AWS profile) and the dev issuer (local profile).
    `issuer` is what tokens carry; `jwks_url` is where this process can fetch keys (they differ inside docker)."""

    _groups_claim = "cognito:groups"

    def __init__(self, issuer: str, jwks_url: str, client_id: str, groups_claim: str = "cognito:groups"):
        self._issuer, self._client_id, self._groups_claim = issuer, client_id, groups_claim
        self._jwks = jwt.PyJWKClient(jwks_url)

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            c = jwt.decode(token, key, algorithms=["RS256"], issuer=self._issuer, options={"verify_aud": False,
                           "require": ["exp", "iss", "sub"]})
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e
        if c.get("token_use") != "access" or c.get("client_id") != self._client_id:
            raise AuthError("wrong token type or client")
        return Principal(c["sub"], c.get("username", c["sub"]), frozenset(c.get(self._groups_claim, [])))


class CognitoVerifier(OidcVerifier):
    def __init__(self, region: str, pool_id: str, client_id: str):
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        super().__init__(issuer, f"{issuer}/.well-known/jwks.json", client_id)
