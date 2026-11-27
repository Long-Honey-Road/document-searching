"""Клиент внутреннего HTTP API knowledge."""

from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError
from regagent_common.contracts import ClauseRequest, ClauseResponse, SearchRequest, SearchResponse

log = logging.getLogger(__name__)


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
