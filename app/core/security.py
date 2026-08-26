"""Auth + tenant resolution.

Two accepted credentials:
  * `Authorization: Bearer <jwt>` for human users coming through the portal
  * `X-API-Key: <key>` for machine callers (ServiceNow/Jira webhooks, schedulers)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import jwt
from fastapi import Depends, Header

from app.core.config import settings
from app.core.errors import AuthError, ForbiddenError


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    roles: list[str] = field(default_factory=list)
    is_service: bool = False

    def require_role(self, *roles: str) -> None:
        if "admin" in self.roles:
            return
        if not set(roles) & set(self.roles):
            raise ForbiddenError(f"requires one of: {', '.join(roles)}")


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Token could not be verified") from exc


async def get_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> Principal:
    if x_api_key:
        if x_api_key not in settings.service_api_keys:
            raise AuthError("Unknown API key")
        return Principal(
            subject=f"service:{x_api_key[:6]}",
            tenant_id=x_tenant_id or "default",
            roles=["service", "agent.invoke", "ingest.write"],
            is_service=True,
        )

    if authorization and authorization.lower().startswith("bearer "):
        claims = _decode_jwt(authorization.split(" ", 1)[1])
        return Principal(
            subject=claims.get("sub", "unknown"),
            tenant_id=claims.get("tenant_id") or x_tenant_id or "default",
            roles=claims.get("roles", ["user"]),
        )

    if settings.app_env == "local":
        # Local dev convenience so you can curl the API without minting tokens.
        return Principal(subject="local-dev", tenant_id=x_tenant_id or "default", roles=["admin"])

    raise AuthError()


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
