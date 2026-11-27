from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from langgraph.store.memory import InMemoryStore
from regagent_common.access import AccessLevels

MakeToken = Callable[..., str]
AEGRA_CONFIG = Path(__file__).resolve().parents[1] / "aegra.json"


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch,
    jwks_dict: dict[str, Any],
    issuer: str,
    audience: str,
) -> AsyncIterator[httpx.AsyncClient]:
    from aegra_api.core.auth_middleware import get_auth_backend
    from aegra_api.core.database import db_manager
    from aegra_api.settings import settings as aegra_settings
    from regagent_backend import auth as auth_module
    from regagent_backend.jwks import JwksCache
    from regagent_backend.memory import MemoryService
    from regagent_backend.settings import BackendSettings

    monkeypatch.setattr(aegra_settings.app, "AEGRA_CONFIG", str(AEGRA_CONFIG))
    monkeypatch.setenv("KEYCLOAK_ISSUER", issuer)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", audience)
    monkeypatch.setenv("KEYCLOAK_JWKS_URL", "http://keycloak.test/certs")
    monkeypatch.setenv("AREA_CODES", "ДИБ,ДБРЦТ,общий")

    from aegra_api.main import create_app

    settings = BackendSettings(_env_file=None)
    store = InMemoryStore()
    memory = MemoryService(store, {"ДИБ", "ДБРЦТ", "общий"}, max_active=3, ttl_days=14)
    await memory.remember("alice", "area", "ДИБ", "stated")
    await memory.remember("bob", "area", "ДБРЦТ", "confirmed")
    monkeypatch.setattr(db_manager, "get_store", lambda: store)

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=jwks_dict))
    async with httpx.AsyncClient(transport=transport) as http:
        jwks = JwksCache(settings.KEYCLOAK_JWKS_URL, http=http, ttl_s=300, min_refresh_s=30)
        levels = AccessLevels({"public": ["*"]})
        authenticate = auth_module.build_authenticator(settings, jwks, levels)
        monkeypatch.setattr(auth_module, "_authenticator", authenticate)
        get_auth_backend.cache_clear()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app()), base_url="http://backend.test"
            ) as client:
                yield client
        finally:
            get_auth_backend.cache_clear()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.leak
async def test_list_returns_only_authenticated_accounts_records(
    client: httpx.AsyncClient, make_token: MakeToken
) -> None:
    response = await client.get(
        "/me/memory", params={"sub": "bob"}, headers=_bearer(make_token(sub="alice"))
    )
    assert response.status_code == 200
    (record,) = response.json()["records"]
    assert record["key"] == "area:ДИБ"
    assert record["kind"] == "area"
    assert record["value"] == "ДИБ"
    assert record["source"] == "stated"
    assert record["times_confirmed"] == 1


@pytest.mark.leak
async def test_delete_is_idempotent_and_scoped_to_the_authenticated_account(
    client: httpx.AsyncClient, make_token: MakeToken
) -> None:
    alice = _bearer(make_token(sub="alice"))
    bob = _bearer(make_token(sub="bob"))
    path = "/me/memory/" + quote("area:ДИБ", safe="")

    assert (await client.delete(path, headers=bob, params={"sub": "alice"})).status_code == 204
    assert (await client.get("/me/memory", headers=alice)).json()["records"][0]["key"] == "area:ДИБ"
    assert (await client.delete(path, headers=alice)).status_code == 204
    assert (await client.delete(path, headers=alice)).status_code == 204
    assert (await client.get("/me/memory", headers=alice)).json() == {"records": []}


@pytest.mark.parametrize("method,path", [("GET", "/me/memory"), ("DELETE", "/me/memory/area:ДИБ")])
@pytest.mark.leak
async def test_memory_routes_reject_missing_expired_or_wrong_issuer_token(
    client: httpx.AsyncClient, make_token: MakeToken, method: str, path: str
) -> None:
    for headers in (
        {},
        _bearer(make_token(exp_in=-600)),
        _bearer(make_token(iss="http://bad.test")),
    ):
        assert (await client.request(method, path, headers=headers)).status_code == 401


@pytest.mark.parametrize("key", ["unknown", "area:", "level:ДИБ"])
async def test_delete_rejects_malformed_key(
    client: httpx.AsyncClient, make_token: MakeToken, key: str
) -> None:
    response = await client.delete(f"/me/memory/{key}", headers=_bearer(make_token()))
    assert response.status_code == 404


@pytest.mark.leak
async def test_memory_routes_do_not_enable_direct_client_store_access(
    client: httpx.AsyncClient, make_token: MakeToken
) -> None:
    response = await client.post(
        "/store/items/search",
        json={"namespace_prefix": ["users", "alice", "memory"]},
        headers=_bearer(make_token(sub="alice")),
    )
    assert response.status_code == 403


async def test_qc_access_comes_from_the_aegra_identity(
    client: httpx.AsyncClient, make_token: MakeToken
) -> None:
    guest = await client.get("/qc/me")
    assert guest.status_code == 401
    employee = await client.get("/qc/me?role=admin", headers=_bearer(make_token(sub="alice")))
    assert employee.status_code == 200 and employee.json()["can_review"] is False
    reviewer = await client.get(
        "/qc/me", headers=_bearer(make_token(sub="admin", realm_access={"roles": ["admin"]}))
    )
    assert reviewer.status_code == 200 and reviewer.json()["can_review"] is True
    assert reviewer.json()["areas"] == ["ДИБ", "ДБРЦТ", "общий"]
