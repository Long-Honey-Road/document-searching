"""Служебные маршруты backend: прокси к /admin knowledge только для роли admin (P2-B3)."""

from __future__ import annotations

import json

import httpx
import pytest

from .test_memory_routes import MakeToken, _bearer
from .test_memory_routes import client as client

JOB = {
    "job_id": "j-1",
    "op": "upsert",
    "doc_id": "finance",
    "version": 2,
    "status": "failed",
    "attempts": 4,
    "error": "сбой",
    "current_step": None,
    "steps": {},
    "created_at": "2026-11-27T10:00:00+07:00",
    "updated_at": "2026-11-27T10:30:00+07:00",
}


@pytest.fixture
def knowledge(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    from regagent_backend import knowledge_client

    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/admin/jobs":
            return httpx.Response(200, json={"jobs": [JOB]})
        if path == "/admin/jobs/j-1/retry":
            return httpx.Response(202, json={"queued": 1})
        if path == "/admin/jobs/gone/retry":
            return httpx.Response(404, json={"detail": "Job not found"})
        if path == "/admin/jobs/busy/retry":
            return httpx.Response(409, json={"detail": "Document is not failed"})
        if path == "/admin/documents":
            return httpx.Response(200, json={"documents": []})
        if path == "/admin/review":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(503)

    monkeypatch.setenv("KNOWLEDGE_INTERNAL_TOKEN", "svc-token")
    monkeypatch.setattr(
        knowledge_client,
        "admin_http",
        lambda timeout_s: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return seen


def _admin(make_token: MakeToken) -> dict[str, str]:
    return _bearer(make_token(sub="root", realm_access={"roles": ["admin"]}))


ROUTES = [
    ("GET", "/admin/documents"),
    ("GET", "/admin/jobs"),
    ("POST", "/admin/jobs/j-1/retry"),
    ("GET", "/admin/review"),
]


@pytest.mark.leak
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_admin_routes_require_admin_role(
    client: httpx.AsyncClient,
    make_token: MakeToken,
    knowledge: list[httpx.Request],
    method: str,
    path: str,
) -> None:
    assert (await client.request(method, path)).status_code == 401
    qc = _bearer(make_token(realm_access={"roles": ["qc"]}))
    assert (await client.request(method, path, headers=qc)).status_code == 403
    assert not knowledge, "без роли admin запрос до knowledge не доходит"
    assert (await client.request(method, path, headers=_admin(make_token))).status_code < 300


async def test_admin_jobs_are_proxied_with_service_token(
    client: httpx.AsyncClient, make_token: MakeToken, knowledge: list[httpx.Request]
) -> None:
    response = await client.get(
        "/admin/jobs", params={"status": "failed", "limit": 5}, headers=_admin(make_token)
    )
    assert response.status_code == 200
    assert response.json()["jobs"][0]["job_id"] == "j-1"
    [request] = knowledge
    assert request.headers["X-Service-Token"] == "svc-token"
    assert "authorization" not in request.headers, "токен пользователя в knowledge не уходит"
    assert dict(request.url.params) == {"status": "failed", "limit": "5"}


async def test_admin_retry_passes_knowledge_outcome(
    client: httpx.AsyncClient, make_token: MakeToken, knowledge: list[httpx.Request]
) -> None:
    admin = _admin(make_token)
    queued = await client.post("/admin/jobs/j-1/retry", headers=admin)
    assert (queued.status_code, queued.json()) == (202, {"queued": 1})
    assert (await client.post("/admin/jobs/gone/retry", headers=admin)).status_code == 404
    assert (await client.post("/admin/jobs/busy/retry", headers=admin)).status_code == 409
    assert [r.method for r in knowledge] == ["POST"] * 3


async def test_admin_knowledge_down_is_502(
    client: httpx.AsyncClient, make_token: MakeToken, monkeypatch: pytest.MonkeyPatch
) -> None:
    from regagent_backend import knowledge_client

    monkeypatch.setenv("KNOWLEDGE_INTERNAL_TOKEN", "svc-token")
    calls: list[httpx.Request] = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        knowledge_client,
        "admin_http",
        lambda timeout_s: httpx.AsyncClient(transport=httpx.MockTransport(down)),
    )
    response = await client.get("/admin/documents", headers=_admin(make_token))
    assert response.status_code == 502
    # Aegra отдаёт HTTPException в своём формате ошибок
    assert json.loads(response.content)["message"] == "knowledge unavailable"
    assert len(calls) == 1, "502 из-за подменённого транспорта, а не настоящей сети"
