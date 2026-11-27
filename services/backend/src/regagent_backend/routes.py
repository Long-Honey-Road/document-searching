"""Маршруты backend, подключаемые Aegra с обязательной аутентификацией."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from aegra_api.core.database import db_manager
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ValidationError
from regagent_common.contracts import AdminDocuments, AdminJobs, AdminReview, RetryResponse

from regagent_backend import knowledge_client
from regagent_backend.knowledge_client import KnowledgeClient, KnowledgeUnavailable
from regagent_backend.memory import MemoryService
from regagent_backend.settings import BackendSettings

ADMIN_ROLE = "admin"

app = FastAPI()


async def _authenticated_sub(request: Request) -> str:
    user = request.scope.get("user")
    sub = getattr(user, "identity", None)
    if not getattr(user, "is_authenticated", False) or not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=401, detail="unauthorized")
    return sub


def _memory() -> MemoryService:
    settings = BackendSettings()
    return MemoryService(
        db_manager.get_store(),
        set(settings.AREA_CODES),
        max_active=settings.MEMORY_MAX_ACTIVE_AREAS,
        ttl_days=settings.MEMORY_AREA_TTL_DAYS,
    )


@app.get("/me/memory")
async def list_memory(
    sub: Annotated[str, Depends(_authenticated_sub)],
) -> dict[str, list[dict[str, object]]]:
    return {
        "records": [
            {"key": record.key, **record.model_dump(mode="json")}
            for record in await _memory().load(sub)
        ]
    }


@app.delete("/me/memory/{key}", status_code=204)
async def delete_memory(key: str, sub: Annotated[str, Depends(_authenticated_sub)]) -> Response:
    kind, separator, value = key.partition(":")
    if not separator:
        raise HTTPException(status_code=404, detail="memory record not found")
    try:
        await _memory().forget(sub, kind, value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="memory record not found") from exc
    return Response(status_code=204)


# --- служебные маршруты: прокси к /admin knowledge, только роль admin (P2-B3) ---


async def _admin_sub(request: Request, sub: Annotated[str, Depends(_authenticated_sub)]) -> str:
    roles = getattr(request.scope.get("user"), "roles", None) or []
    if ADMIN_ROLE not in roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return sub


async def _knowledge_admin[Model: BaseModel](
    method: str,
    path: str,
    model: type[Model],
    *,
    params: dict[str, str | int] | None = None,
    expected: int = 200,
) -> Model:
    settings = BackendSettings()
    token = settings.KNOWLEDGE_INTERNAL_TOKEN.get_secret_value()
    async with knowledge_client.admin_http(settings.KNOWLEDGE_TIMEOUT_S) as http:
        try:
            response = await KnowledgeClient(settings.KNOWLEDGE_URL, token, http).admin(
                method, path, params
            )
        except KnowledgeUnavailable as exc:
            raise HTTPException(status_code=502, detail="knowledge unavailable") from exc
    if response.status_code in {404, 409, 422}:
        raise HTTPException(status_code=response.status_code, detail="rejected by knowledge")
    if response.status_code != expected:
        raise HTTPException(status_code=502, detail="knowledge unavailable")
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail="knowledge unavailable") from exc


AdminSub = Annotated[str, Depends(_admin_sub)]


@app.get("/admin/documents")
async def admin_documents(_: AdminSub) -> AdminDocuments:
    return await _knowledge_admin("GET", "/admin/documents", AdminDocuments)


@app.get("/admin/jobs")
async def admin_jobs(
    _: AdminSub,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AdminJobs:
    params: dict[str, str | int] = {"limit": limit}
    if status is not None:
        params["status"] = status
    return await _knowledge_admin("GET", "/admin/jobs", AdminJobs, params=params)


@app.post("/admin/jobs/{job_id}/retry", status_code=202)
async def admin_retry(job_id: str, _: AdminSub) -> RetryResponse:
    path = f"/admin/jobs/{quote(job_id, safe='')}/retry"
    return await _knowledge_admin("POST", path, RetryResponse, expected=202)


@app.get("/admin/review")
async def admin_review(_: AdminSub) -> AdminReview:
    return await _knowledge_admin("GET", "/admin/review", AdminReview)
