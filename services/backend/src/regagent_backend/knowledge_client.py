"""Клиент внутреннего HTTP API knowledge."""

from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError
from regagent_common.contracts import (
    ClauseRequest,
    ClauseResponse,
    RefsRequest,
    RefsResponse,
    SearchRequest,
    SearchResponse,
)

log = logging.getLogger(__name__)


def admin_http(timeout_s: float) -> httpx.AsyncClient:
    """HTTP-клиент служебных маршрутов. Отдельная функция модуля: Aegra загружает routes.py
    по пути файла, поэтому подменять транспорт в тестах можно только здесь."""
    return httpx.AsyncClient(timeout=timeout_s)


class KnowledgeUnavailable(Exception):
    """Поиск или чтение недоступны."""


class KnowledgeClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Service-Token": token}
        self.http = http

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            response = await self.http.post(
                self.base_url + "/search", json=request.model_dump(), headers=self.headers
            )
            response.raise_for_status()
            return SearchResponse.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as exc:
            log.warning("knowledge search unavailable: %s", type(exc).__name__)
            raise KnowledgeUnavailable("search unavailable") from exc

    async def clause(self, request: ClauseRequest) -> ClauseResponse | None:
        try:
            response = await self.http.post(
                self.base_url + "/clause", json=request.model_dump(), headers=self.headers
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return ClauseResponse.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as exc:
            log.warning("knowledge clause unavailable: %s", type(exc).__name__)
            raise KnowledgeUnavailable("clause unavailable") from exc

    async def refs(self, request: RefsRequest) -> RefsResponse | None:
        """Связанные документы; ``None`` — документа нет или он недоступен (одинаково)."""
        try:
            response = await self.http.post(
                self.base_url + "/refs", json=request.model_dump(), headers=self.headers
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return RefsResponse.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as exc:
            log.warning("knowledge refs unavailable: %s", type(exc).__name__)
            raise KnowledgeUnavailable("refs unavailable") from exc

    async def admin(
        self, method: str, path: str, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        """Служебный маршрут knowledge как есть; сетевые ошибки — ``KnowledgeUnavailable``."""
        if not path.startswith("/admin/"):
            raise ValueError("only /admin routes")
        try:
            return await self.http.request(
                method, self.base_url + path, params=params, headers=self.headers
            )
        except httpx.HTTPError as exc:
            log.warning("knowledge admin unavailable: %s", type(exc).__name__)
            raise KnowledgeUnavailable("admin unavailable") from exc
