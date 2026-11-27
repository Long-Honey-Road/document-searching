"""Идентичность пользователя: проверка JWT Keycloak и чтение пользователя внутри графа.

Права определяет только этот код: уровни доступа вычисляются из групп токена и дальше
берутся только из объекта пользователя, который выставил сервер (D-17).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

log = logging.getLogger(__name__)

# Только асимметричные алгоритмы: симметричный HS* с публичным ключом — классическая подмена.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")


class AuthError(Exception):
    """Токен не принят. Текст не показывается пользователю."""


class LevelsResolver(Protocol):
    """Группы Keycloak → уровни доступа (`AccessLevels` из regagent-common)."""

    def levels_for(self, groups: Iterable[str]) -> frozenset[str]: ...


@dataclass(frozen=True)
class UserIdentity:
    sub: str
    username: str
    groups: frozenset[str]
    roles: frozenset[str]
    levels: frozenset[str]


def _str_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, str) and v)


def verify_jwt(
    token: str,
    jwks: jwt.PyJWKSet,
    *,
    issuer: str,
    audience: str,
    levels: LevelsResolver,
    leeway_s: float = 0,
) -> UserIdentity:
    """Подпись по JWKS, `iss`, `aud`, `exp`; группы → уровни. Любая ошибка — `AuthError`."""
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            raise AuthError(f"algorithm not allowed: {alg!r}")
        kid = header.get("kid")
        key = next((k for k in jwks.keys if k.key_id == kid), None)
        if key is None:
            raise AuthError("unknown kid")
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=[alg],
            issuer=issuer,
            audience=audience,
            leeway=leeway_s,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(type(exc).__name__) from exc

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("empty sub")

    raw_groups = claims.get("groups")
    if raw_groups is not None and not isinstance(raw_groups, list):
        log.warning("claim groups is not a list for sub=%s; treated as empty", sub)
    groups = _str_set(raw_groups)

    realm_access = claims.get("realm_access")
    roles = _str_set(realm_access.get("roles") if isinstance(realm_access, Mapping) else None)

    username = claims.get("preferred_username")
    return UserIdentity(
        sub=sub,
        username=username if isinstance(username, str) and username else sub,
        groups=groups,
        roles=roles,
        levels=frozenset(levels.levels_for(groups)),
    )


def identity_to_user_dict(ident: UserIdentity) -> dict[str, Any]:
    """Словарь для обработчика аутентификации Aegra; поля станут атрибутами пользователя."""
    return {
        "identity": ident.sub,
        "display_name": ident.username,
        "permissions": sorted(ident.roles),
        "is_authenticated": True,
        "groups": sorted(ident.groups),
        "roles": sorted(ident.roles),
        "levels": sorted(ident.levels),
    }


def _field(user: Any, name: str) -> Any:
    if isinstance(user, Mapping):
        return user.get(name)
    return getattr(user, name, None)


def current_identity(config: Mapping[str, Any] | None = None) -> UserIdentity:
    """Пользователь текущего запуска графа.

    Берётся только из `configurable.langgraph_auth_user` — его сервер перезаписывает всегда.
    Остальные ключи `configurable` (включая `user_id`) приходят от клиента и игнорируются.
    Нет пользователя или уровней — `AuthError` (fail closed).
    """
    if config is None:
        from langgraph.config import get_config

        config = get_config()
    configurable = config.get("configurable") or {}
    return identity_from_auth_user(configurable.get("langgraph_auth_user"))


def identity_from_auth_user(user: Any) -> UserIdentity:
    if user is None:
        raise AuthError("no authenticated user in run config")

    sub = _field(user, "identity")
    levels = _field(user, "levels")
    if not isinstance(sub, str) or not sub or not isinstance(levels, list | tuple | frozenset):
        raise AuthError("incomplete authenticated user")

    username = _field(user, "display_name")
    return UserIdentity(
        sub=sub,
        username=username if isinstance(username, str) and username else sub,
        groups=_str_set(list(_field(user, "groups") or [])),
        roles=_str_set(list(_field(user, "roles") or [])),
        levels=_str_set(list(levels)),
    )
