"""Маршруты backend, подключаемые Aegra с обязательной аутентификацией."""

from __future__ import annotations

from typing import Annotated

from aegra_api.core.database import db_manager
from fastapi import Depends, FastAPI, HTTPException, Request, Response

from regagent_backend.memory import MemoryService
from regagent_backend.settings import BackendSettings

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
