"""Кэш ключей Keycloak (JWKS) с обновлением по сроку и при смене ключа."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx
import jwt

log = logging.getLogger(__name__)


class JwksUnavailable(Exception):
    """Не удалось получить ключи Keycloak."""


class JwksCache:
    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient,
        ttl_s: float,
        min_refresh_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._http = http
        self._ttl = ttl_s
        self._min_refresh = min_refresh_s
        self._clock = clock
        self._jwks: jwt.PyJWKSet | None = None
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def get(self, kid: str | None = None) -> jwt.PyJWKSet:
        """Ключи; при неизвестном `kid` — внеочередное обновление не чаще `min_refresh_s`."""
        if self._needs_refresh(kid):
            async with self._lock:
                if self._needs_refresh(kid):  # другой запрос мог обновить, пока ждали
                    await self._refresh()
        if self._jwks is None:
            raise JwksUnavailable
        return self._jwks

    def _needs_refresh(self, kid: str | None) -> bool:
        age = self._clock() - self._fetched_at
        if self._jwks is None:
            # Keycloak недоступен: не долбим его на каждый запрос
            return age >= self._min_refresh
        if age >= self._ttl:
            return True
        unknown = kid is not None and all(k.key_id != kid for k in self._jwks.keys)
        return unknown and age >= self._min_refresh

    async def _refresh(self) -> None:
        try:
            resp = await self._http.get(self._url)
            resp.raise_for_status()
            self._jwks = jwt.PyJWKSet.from_dict(resp.json())
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            log.error("JWKS fetch failed: %s", type(exc).__name__)
            if self._jwks is None:
                raise JwksUnavailable from exc
            return  # оставляем прежние ключи до следующей попытки
        finally:
            self._fetched_at = self._clock()
