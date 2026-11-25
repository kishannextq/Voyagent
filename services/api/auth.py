"""Authentication (Cognito access tokens) and the caller's identity (SECURITY.md F2).

AUTH_MODE:
- cognito: every protected route needs `Authorization: Bearer <Cognito access token>`.
  Verified locally against the user pool's JWKS (RS256): issuer, expiry, token_use=access,
  and client_id in COGNITO_CLIENT_IDS.
- disabled: a fixed local user. Allowed only when APP_ENV is local or test.

Identity never comes from the request body. Tenant comes from a token claim
(AUTH_TENANT_CLAIM, set by a pre-token-generation trigger); without it each user is their
own tenant. Roles come from Cognito groups: `admin` (read any plan in its tenant), else member.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field

import jwt
from fastapi import HTTPException, Request

import settings

log = logging.getLogger("voyagent.auth")

ROLES = {"admin", "member"}


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"member"}))

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


LOCAL_PRINCIPAL = Principal(sub="local-user", tenant_id="local")


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    issuer: str = ""
    client_ids: frozenset[str] = frozenset()
    tenant_claim: str = "custom:tenant_id"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> "AuthConfig":
        default = "disabled" if not settings.durable_required() else "cognito"
        mode = os.getenv("AUTH_MODE", default).strip().lower()
        if mode not in {"cognito", "disabled"}:
            raise RuntimeError("AUTH_MODE must be 'cognito' or 'disabled'")
        if mode == "disabled":
            if settings.durable_required():
                raise RuntimeError(f"AUTH_MODE=disabled is not allowed when APP_ENV={settings.app_env()}")
            return cls(mode="disabled")
        region = os.getenv("COGNITO_REGION", "").strip()
        pool = os.getenv("COGNITO_USER_POOL_ID", "").strip()
        clients = frozenset(c.strip() for c in os.getenv("COGNITO_CLIENT_IDS", "").split(",") if c.strip())
        issuer = os.getenv("COGNITO_ISSUER", "").strip() or (
            f"https://cognito-idp.{region}.amazonaws.com/{pool}" if region and pool else ""
        )
        if not issuer or not clients:
            raise RuntimeError(
                "AUTH_MODE=cognito needs COGNITO_REGION + COGNITO_USER_POOL_ID (or COGNITO_ISSUER) and COGNITO_CLIENT_IDS"
            )
        return cls(
            mode="cognito",
            issuer=issuer.rstrip("/"),
            client_ids=clients,
            tenant_claim=os.getenv("AUTH_TENANT_CLAIM", "custom:tenant_id").strip(),
        )


class InvalidToken(Exception):
    """Reason is for logs only; clients get a generic 401."""


class Verifier:
    def __init__(self, config: AuthConfig, jwk_client=None):
        self.config = config
        # Caches signing keys; refetches on an unknown key id (key rotation).
        self._jwks = jwk_client or jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600, timeout=5)

    async def verify(self, token: str) -> Principal:
        try:
            # May fetch the JWKS over the network on a cache miss: keep it off the event loop.
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "iss"], "verify_aud": False},
            )
        except jwt.PyJWKClientError as e:
            raise InvalidToken(f"signing key: {type(e).__name__}") from None
        except jwt.InvalidTokenError as e:
            raise InvalidToken(type(e).__name__) from None
        # Cognito access tokens carry client_id (not aud) and token_use=access.
        if claims.get("token_use") != "access":
            raise InvalidToken("token_use")
        if claims.get("client_id") not in self.config.client_ids:
            raise InvalidToken("client_id")
        sub = str(claims["sub"])
        tenant = str(claims.get(self.config.tenant_claim) or f"user:{sub}")
        groups = claims.get("cognito:groups") or []
        roles = frozenset(g for g in groups if g in ROLES) or frozenset({"member"})
        return Principal(sub=sub, tenant_id=tenant, roles=roles)


class Authenticator:
    def __init__(self, config: AuthConfig, verifier: Verifier | None = None):
        self.config = config
        self.verifier = verifier or (Verifier(config) if config.mode == "cognito" else None)

    async def __call__(self, request: Request) -> Principal:
        if self.config.mode == "disabled":
            principal = LOCAL_PRINCIPAL
        else:
            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise _unauthorized()
            try:
                principal = await self.verifier.verify(token.strip())
            except InvalidToken as e:
                log.warning("auth_rejected", extra={"reason": str(e)})
                raise _unauthorized() from None
        request.state.principal = principal
        return principal


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})


def can_access(principal: Principal, owner_tenant: str, owner_sub: str) -> bool:
    """Owners can access their plans; tenant admins can access any plan in their tenant."""
    if principal.tenant_id != owner_tenant:
        return False
    return principal.sub == owner_sub or principal.is_admin
