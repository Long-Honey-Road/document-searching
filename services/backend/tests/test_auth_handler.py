from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from langgraph_sdk import Auth
from regagent_backend.auth import build_authenticator, deny_client_store
from regagent_backend.jwks import JwksCache
from regagent_backend.settings import BackendSettings

MakeToken = Callable[..., str]
JWKS_URL = "http://keycloak:8080/auth/realms/regagent/protocol/openid-connect/certs"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, issuer: str, audience: str) -> BackendSettings:
    monkeypatch.setenv("KEYCLOAK_ISSUER", issuer)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", audience)
    monkeypatch.setenv("KEYCLOAK_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("AREA_CODES", "ДИБ,ДБРЦТ,общий")
    return BackendSettings(_env_file=None)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def jwks_calls() -> list[str]:
    return []


@pytest.fixture
def jwks_cache(
    jwks_dict: dict[str, Any], jwks_calls: list[str], clock: Clock, settings: BackendSettings
) -> JwksCache:
    def handler(request: httpx.Request) -> httpx.Response:
        jwks_calls.append(str(request.url))
        return httpx.Response(200, json=jwks_dict)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JwksCache(
        settings.KEYCLOAK_JWKS_URL,
        http=http,
        ttl_s=settings.JWKS_CACHE_TTL_S,
        min_refresh_s=settings.JWKS_MIN_REFRESH_S,
        clock=clock,
    )


@pytest.fixture
def authenticate(
    settings: BackendSettings, jwks_cache: JwksCache, levels: Any
) -> Callable[[dict[str, str]], Any]:
    return build_authenticator(settings, jwks_cache, levels)


async def test_valid_bearer_returns_user_dict(
    authenticate: Any, make_token: MakeToken, jwks_calls: list[str]
) -> None:
    user = await authenticate(
        {"authorization": "Bearer " + make_token(sub="u-7", groups=["/levels/closed_fin"])}
    )
    assert user["identity"] == "u-7"
    assert user["levels"] == ["closed_fin", "public"]
    assert jwks_calls == [JWKS_URL]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": ""},
        {"authorization": "Basic abc"},
        {"authorization": "Bearer"},
        {"authorization": "Bearer not-a-jwt"},
    ],
)
async def test_missing_or_bad_header_is_401(authenticate: Any, headers: dict[str, str]) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as err:
        await authenticate(headers)
    assert err.value.status_code == 401


async def test_expired_token_is_401(authenticate: Any, make_token: MakeToken) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as err:
        await authenticate({"authorization": "Bearer " + make_token(exp_in=-600)})
    assert err.value.status_code == 401


async def test_jwks_cached_between_requests(
    authenticate: Any, make_token: MakeToken, jwks_calls: list[str], clock: Clock
) -> None:
    for _ in range(3):
        await authenticate({"authorization": "Bearer " + make_token()})
        clock.now += 1
    assert len(jwks_calls) == 1


async def test_jwks_refreshed_after_ttl(
    authenticate: Any,
    make_token: MakeToken,
    jwks_calls: list[str],
    clock: Clock,
    settings: BackendSettings,
) -> None:
    await authenticate({"authorization": "Bearer " + make_token()})
    clock.now += settings.JWKS_CACHE_TTL_S + 1
    await authenticate({"authorization": "Bearer " + make_token()})
    assert len(jwks_calls) == 2


async def test_unknown_kid_refreshes_jwks_but_not_on_every_request(
    authenticate: Any,
    make_token: MakeToken,
    jwks_calls: list[str],
    clock: Clock,
    settings: BackendSettings,
) -> None:
    await authenticate({"authorization": "Bearer " + make_token()})
    with pytest.raises(Auth.exceptions.HTTPException):
        await authenticate({"authorization": "Bearer " + make_token(kid="rotated")})
    assert len(jwks_calls) == 1  # только что загружали — не дёргаем Keycloak
    clock.now += settings.JWKS_MIN_REFRESH_S + 1
    for _ in range(5):
        with pytest.raises(Auth.exceptions.HTTPException):
            await authenticate({"authorization": "Bearer " + make_token(kid="rotated")})
    # одна повторная загрузка на неизвестный kid, дальше — не чаще min_refresh_s
    assert len(jwks_calls) == 2


async def test_jwks_unavailable_is_401(
    settings: BackendSettings, levels: Any, make_token: MakeToken
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    cache = JwksCache(
        settings.KEYCLOAK_JWKS_URL,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ttl_s=60,
        min_refresh_s=10,
    )
    authenticate = build_authenticator(settings, cache, levels)
    with pytest.raises(Auth.exceptions.HTTPException) as err:
        await authenticate({"authorization": "Bearer " + make_token()})
    assert err.value.status_code == 401


async def test_client_store_access_denied() -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as err:
        await deny_client_store(None, {"namespace": ["memory"], "key": "area:ДИБ"})  # type: ignore[arg-type]
    assert err.value.status_code == 403


def test_area_codes_parsed_from_comma_list(settings: BackendSettings) -> None:
    assert settings.AREA_CODES == ["ДИБ", "ДБРЦТ", "общий"]


async def test_keycloak_down_is_not_hammered(
    settings: BackendSettings, levels: Any, make_token: MakeToken, clock: Clock
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    cache = JwksCache(
        settings.KEYCLOAK_JWKS_URL,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ttl_s=60,
        min_refresh_s=10,
        clock=clock,
    )
    authenticate = build_authenticator(settings, cache, levels)
    for _ in range(5):
        with pytest.raises(Auth.exceptions.HTTPException):
            await authenticate({"authorization": "Bearer " + make_token()})
    assert len(calls) == 1
    clock.now += 11
    with pytest.raises(Auth.exceptions.HTTPException):
        await authenticate({"authorization": "Bearer " + make_token()})
    assert len(calls) == 2
