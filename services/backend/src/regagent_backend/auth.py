"""Аутентификация Aegra: `aegra.json` → `"auth": {"path": "regagent_backend.auth:auth"}`.

Aegra 0.12 вызывает обработчик с одним позиционным `dict` заголовков (D-17).
Любая ошибка — 401 без подробностей; подробности — только в лог.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from langgraph_sdk import Auth

from regagent_backend.identity import (
    AuthError,
    LevelsResolver,
    identity_to_user_dict,
    verify_jwt,
)
from regagent_backend.jwks import JwksCache, JwksUnavailable
from regagent_backend.settings import BackendSettings

log = logging.getLogger(__name__)

Authenticator = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]


def _unauthorized() -> Auth.exceptions.HTTPException:
    return Auth.exceptions.HTTPException(status_code=401, detail="unauthorized")


def _bearer(headers: dict[str, str]) -> str:
    value = headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    return token.strip()


def build_authenticator(
    settings: BackendSettings, jwks: JwksCache, levels: LevelsResolver
) -> Authenticator:
    async def authenticate(headers: dict[str, str]) -> dict[str, Any]:
        token = _bearer(headers)
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            keys = await jwks.get(kid)
            ident = verify_jwt(
                token,
                keys,
                issuer=settings.KEYCLOAK_ISSUER,
                audience=settings.KEYCLOAK_AUDIENCE,
                levels=levels,
                leeway_s=settings.JWT_LEEWAY_S,
            )
        except (AuthError, JwksUnavailable, jwt.PyJWTError) as exc:
            log.info("token rejected: %s", exc)
            raise _unauthorized() from exc
        return identity_to_user_dict(ident)

    return authenticate


async def deny_client_store(ctx: Auth.types.AuthContext, value: dict[str, Any]) -> None:
    """Прямой REST /store клиенту закрыт: память — только через граф и /me/memory (D-18)."""
    raise Auth.exceptions.HTTPException(status_code=403, detail="forbidden")


def _load_levels(settings: BackendSettings) -> LevelsResolver:
    from regagent_common.access import AccessLevels  # P0-2

    return AccessLevels.from_yaml(settings.ACCESS_LEVELS_FILE)


_authenticator: Authenticator | None = None


async def _default_authenticator(headers: dict[str, str]) -> dict[str, Any]:
    global _authenticator
    if _authenticator is None:
        settings = BackendSettings()  # значения из .env
        cache = JwksCache(
            settings.KEYCLOAK_JWKS_URL,
            http=httpx.AsyncClient(timeout=10),
            ttl_s=settings.JWKS_CACHE_TTL_S,
            min_refresh_s=settings.JWKS_MIN_REFRESH_S,
        )
        _authenticator = build_authenticator(settings, cache, _load_levels(settings))
    return await _authenticator(headers)


auth = Auth()
auth.authenticate(_default_authenticator)
auth.on.store(deny_client_store)
