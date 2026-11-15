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


class CognitoVerifier:
    def __init__(self, region: str, pool_id: str, client_id: str):
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        self._client_id = client_id
        self._jwks = jwt.PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            c = jwt.decode(token, key, algorithms=["RS256"], issuer=self._issuer, options={"verify_aud": False})
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e
        if c.get("token_use") != "access" or c.get("client_id") != self._client_id:
            raise AuthError("wrong token type or client")
        return Principal(c["sub"], c.get("username", c["sub"]), frozenset(c.get("cognito:groups", [])))
