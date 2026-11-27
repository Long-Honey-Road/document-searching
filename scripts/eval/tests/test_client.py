import asyncio
import json
import secrets
import time

import httpx
import jwt
import pytest
from pydantic import SecretStr

from scripts.eval.client import Login, StandError, StandSession, Turn


def values(*messages):
    return "event: values\ndata: " + json.dumps({"messages": messages}) + "\n\n"


@pytest.mark.asyncio
async def test_reuses_keycloak_login_and_sends_bearer_to_langgraph(monkeypatch):
    from tests.e2e import test_mvp_scenarios

    claims = {"exp": int(time.time()) + 300}
    token = jwt.encode(claims, secrets.token_urlsafe(48), algorithm="HS256")
    base = "http://localhost:8080"
    issuer = base + "/auth/realms/regagent"
    secret = SecretStr(secrets.token_urlsafe(16))
    logins = []

    def login(*args):
        assert args == (issuer, "regagent-web", base, "employee_fin", secret.get_secret_value())
        logins.append(True)
        return token

    def transport(request):
        assert request.url.path == "/api/assistants/search"
        assert request.headers["authorization"] == "Bearer " + token
        return httpx.Response(200, json=[{"assistant_id": "assistant"}])

    monkeypatch.setattr(test_mvp_scenarios, "sign_in", login)
    async with httpx.AsyncClient(
        base_url=base + "/api/", transport=httpx.MockTransport(transport)
    ) as http:
        session = StandSession(http, None)
        account = Login(base, issuer, "regagent-web", "employee_fin", secret)
        await session.sign_in(account)
        await session.sign_in(account)
    assert len(logins) == 1
    assert session.assistant == "assistant"


@pytest.mark.asyncio
async def test_stream_returns_new_answer_metadata_and_both_timings(monkeypatch):
    old = {"type": "ai", "content": "Предыдущий ответ"}
    meta = {"verified": True, "outcome": "answer", "sources": []}
    answer = {"type": "ai", "content": "Ответ", "additional_kwargs": {"answer_meta": meta}}
    clock = iter([10.0, 10.3, 10.5])
    monkeypatch.setattr("scripts.eval.client.monotonic", lambda: next(clock))

    def transport(request):
        assert request.url.path == "/api/threads/thread/runs/stream"
        payload = json.loads(request.content)
        sent = payload["input"]["messages"][-1]
        assert sent["type"] == "human" and sent["content"] == "Новый вопрос"
        assert payload["stream_mode"] == "values"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=values(old) + values(old, sent) + values(old, sent, answer) + "event: end\n\n",
        )

    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/", transport=httpx.MockTransport(transport)
    ) as http:
        answer = await StandSession(http, "assistant").ask("thread", "Новый вопрос", 2)
    assert answer.text == "Ответ"
    assert answer.meta == meta
    assert answer.first_words_s == pytest.approx(0.3)
    assert answer.total_s == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reset_deletes_encoded_keys_and_verifies_empty_memory():
    paths = []

    def transport(request):
        paths.append((request.method, request.url.raw_path))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200, json={"records": [{"key": "area:ДИБ"}] if len(paths) == 1 else []}
        )

    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/", transport=httpx.MockTransport(transport)
    ) as http:
        await StandSession(http, "assistant").reset_memory()
    assert paths == [
        ("GET", b"/api/me/memory"),
        ("DELETE", b"/api/me/memory/area%3A%D0%94%D0%98%D0%91"),
        ("GET", b"/api/me/memory"),
    ]


@pytest.mark.asyncio
async def test_reset_reports_remaining_memory():
    def transport(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"records": [{"key": "area:ДИБ"}]})

    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/", transport=httpx.MockTransport(transport)
    ) as http:
        with pytest.raises(StandError, match="память осталась"):
            await StandSession(http, "assistant").reset_memory()


@pytest.mark.asyncio
async def test_stream_times_out_even_when_transport_does_not_finish():
    async def transport(_request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        base_url="http://localhost:8080/api/", transport=httpx.MockTransport(transport)
    ) as http:
        with pytest.raises(StandError, match="не завершился"):
            await StandSession(http, "assistant").ask("thread", "Вопрос", 0.01)


def test_error_stream_does_not_surface_response_payload():
    turn = Turn(start=0)
    with pytest.raises(StandError, match="ошибке выполнения") as exc:
        turn.add("error", {"message": "secret"})
    assert "secret" not in str(exc.value)
