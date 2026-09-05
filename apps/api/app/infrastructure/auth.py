"""Token verification (spec section 4: buy authentication, do not build).

No user store, no password, no session lives in this application. The identity
provider (Auth0, WorkOS, Clerk, or any OIDC issuer) signs a JWT; this module
verifies it and projects the claims onto a :class:`Principal`.

Two verification modes:

* **JWKS** - production. RS256/ES256 verified against the issuer's published
  keys, with issuer and audience checked.
* **dev secret** - local development and tests only. HS256 against
  ``AUTH_DEV_SECRET``. ``Settings`` refuses to start in production when that
  variable is set, so this path cannot reach a real deployment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import jwt
from jwt import PyJWKClient

from apps.api.app.config import Settings
from packages.controls.permissions import Principal
from packages.domain.enums import ActorType, Role

__all__ = ["AuthError", "TokenVerifier", "principal_from_claims"]

# Claim names carrying the platform's own authorisation data. Namespaced,
# because a bare "roles" claim is easy for another system to collide with.
ROLES_CLAIM = "https://recon-platform.example/roles"
TENANT_CLAIM = "https://recon-platform.example/tenant_id"
APPROVAL_LIMIT_CLAIM = "https://recon-platform.example/approval_limit"

_LEEWAY_SECONDS = 30


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired or untrusted."""

    def __init__(self, message: str, code: str = "invalid_token") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class TokenVerifier:
    settings: Settings
    _jwks_client: PyJWKClient | None = field(default=None, init=False, repr=False)

    def verify(self, token: str) -> dict[str, Any]:
        """Verify a bearer token and return its claims."""
        if not token:
            raise AuthError("No bearer token was supplied.", "missing_token")

        if self.settings.auth_jwks_url:
            return self._verify_jwks(token)
        if self.settings.auth_dev_secret:
            return self._verify_dev(token)
        raise AuthError(
            "No authentication method is configured. Set AUTH_JWKS_URL for a "
            "real identity provider.",
            "not_configured",
        )

    def _verify_jwks(self, token: str) -> dict[str, Any]:
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(self.settings.auth_jwks_url)
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return dict(
                jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256", "RS512", "ES256"],
                    audience=self.settings.auth_audience,
                    issuer=self.settings.auth_issuer or None,
                    leeway=_LEEWAY_SECONDS,
                    options={"require": ["exp", "iat", "sub"]},
                )
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token rejected: {exc}") from exc

    def _verify_dev(self, token: str) -> dict[str, Any]:
        try:
            return dict(
                jwt.decode(
                    token,
                    self.settings.auth_dev_secret,
                    algorithms=["HS256"],
                    audience=self.settings.auth_audience or None,
                    options={
                        "require": ["exp", "sub"],
                        "verify_aud": bool(self.settings.auth_audience),
                    },
                    leeway=_LEEWAY_SECONDS,
                )
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token rejected: {exc}") from exc


def principal_from_claims(claims: dict[str, Any], *, user_id: UUID) -> Principal:
    """Project verified claims onto a :class:`Principal`.

    Unknown role names are dropped rather than causing a failure: an IdP that
    grows a new group must not lock everyone out, and a role this platform does
    not recognise grants nothing anyway.
    """
    tenant_raw = claims.get(TENANT_CLAIM) or claims.get("tenant_id")
    if not tenant_raw:
        raise AuthError("The token carries no tenant claim.", "missing_tenant")
    try:
        tenant_id = UUID(str(tenant_raw))
    except ValueError as exc:
        raise AuthError("The tenant claim is not a valid UUID.", "invalid_tenant") from exc

    raw_roles = claims.get(ROLES_CLAIM) or claims.get("roles") or []
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]

    roles: set[Role] = set()
    for name in raw_roles:
        try:
            roles.add(Role(str(name).upper()))
        except ValueError:
            continue

    approval_limit = claims.get(APPROVAL_LIMIT_CLAIM)
    limit = int(approval_limit) if approval_limit is not None else None

    return Principal(
        tenant_id=tenant_id,
        actor_type=ActorType.USER,
        user_id=user_id,
        email=str(claims.get("email") or "") or None,
        roles=frozenset(roles),
        approval_limit=limit,
    )


def issue_dev_token(
    settings: Settings,
    *,
    subject: str,
    tenant_id: UUID,
    roles: list[str],
    email: str = "dev@example.com",
    approval_limit: int | None = None,
    ttl_seconds: int = 3600,
) -> str:
    """Mint a development token.

    Used by ``scripts/seed_demo.py`` and by the test suite so the whole API can
    be exercised without provisioning an identity provider. It refuses to run
    without ``AUTH_DEV_SECRET``, which production forbids.
    """
    if not settings.auth_dev_secret:
        raise AuthError(
            "AUTH_DEV_SECRET is not set; development tokens are unavailable.",
            "not_configured",
        )
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "iat": now,
        "exp": now + ttl_seconds,
        TENANT_CLAIM: str(tenant_id),
        ROLES_CLAIM: roles,
    }
    if settings.auth_audience:
        claims["aud"] = settings.auth_audience
    if approval_limit is not None:
        claims[APPROVAL_LIMIT_CLAIM] = approval_limit
    return jwt.encode(claims, settings.auth_dev_secret, algorithm="HS256")
