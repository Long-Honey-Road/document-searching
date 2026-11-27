"""Клиент прогона с входом через Keycloak и замером публичного стрима Aegra."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import httpx
import jwt
from langgraph_sdk.client import LangGraphClient
from langgraph_sdk.schema import Input
from pydantic import BaseModel, SecretStr

from scripts.eval.checks import TimedAnswer
from scripts.eval.questions import Account


class StandError(RuntimeError):
    pass


@dataclass
class Turn:
    start: float
    message_id: str = ""
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    first_words_s: float | None = None

    def add(self, event: str, data: Any) -> None:
        if event == "error":
            raise StandError("Стенд сообщил об ошибке выполнения запроса")
        if event != "values" or not isinstance(data, dict):
            return
        messages = data.get("messages", [])
        if not isinstance(messages, list) or len(messages) < 2:
            return
        question, answer = messages[-2:]
        if (
            not isinstance(question, dict)
            or question.get("id") != self.message_id
            or not isinstance(answer, dict)
            or answer.get("type") != "ai"
        ):
            return

        content = answer.get("content")
        if isinstance(content, str):
            self.text = content
        elif isinstance(content, list):
            self.text = "".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        extra = answer.get("additional_kwargs")
        if isinstance(extra, dict):
            meta = extra.get("answer_meta", extra)
            self.meta = meta if isinstance(meta, dict) else {}
        else:
            self.meta = {}
        if self.text.strip() and self.first_words_s is None:
            self.first_words_s = monotonic() - self.start


@dataclass(frozen=True)
class Login:
    base: str
    issuer: str
    audience: str
    user: Account
    password: SecretStr


class MemoryRecord(BaseModel):
    key: str


class MemoryList(BaseModel):
    records: list[MemoryRecord]


class StandSession:
    def __init__(self, http: httpx.AsyncClient, assistant: str | None) -> None:
        self.http = http
        self.client = LangGraphClient(http)
        self.assistant = assistant
        self.tokens: set[str] = set()
        self._renew_at = 0.0

    async def sign_in(self, login: Login) -> None:
        from tests.e2e.test_mvp_scenarios import sign_in

        if time() < self._renew_at:
            return
        try:
            token = await asyncio.to_thread(
                sign_in,
                login.issuer,
                login.audience,
                login.base,
                login.user,
                login.password.get_secret_value(),
            )
            # Сервер проверяет JWT. Здесь нужен лишь срок жизни для обновления сессии.
            expiry = float(jwt.decode(token, options={"verify_signature": False})["exp"])
        except (AssertionError, KeyError, ValueError, TypeError, httpx.HTTPError, jwt.PyJWTError):
            raise StandError(f"Не удалось войти в Keycloak под {login.user}") from None
        self.tokens.add(token)
        self._renew_at = expiry - 30
        self.http.headers["Authorization"] = "Bearer " + token
        if not self.assistant:
            assistants = await self.client.assistants.search(limit=2)
            if len(assistants) != 1:
                raise StandError("Укажите VITE_ASSISTANT_ID в .env: ожидался один ассистент")
            self.assistant = assistants[0]["assistant_id"]

    async def reset_memory(self) -> None:
        for record in await self._read_memory():
            response = await self.http.delete("me/memory/" + quote(record.key, safe=""), timeout=15)
            response.raise_for_status()
        if await self._read_memory():
            raise StandError("После удаления память осталась непустой")

    async def _read_memory(self) -> list[MemoryRecord]:
        response = await self.http.get("me/memory", timeout=15)
        response.raise_for_status()
        try:
            return MemoryList.model_validate(response.json()).records
        except ValueError:
            raise StandError("GET /api/me/memory вернул некорректный список записей") from None

    async def new_thread(self) -> str:
        result = await self.client.threads.create()
        return result["thread_id"]

    async def ask(self, thread: str, question: str, deadline: float) -> TimedAnswer:
        if not self.assistant:
            raise StandError("Ассистент не выбран")
        message_id = str(uuid4())
        payload = cast(
            Input, {"messages": [{"id": message_id, "type": "human", "content": question}]}
        )
        turn = Turn(monotonic(), message_id)
        try:
            async with asyncio.timeout(deadline):
                async for chunk in self.client.runs.stream(
                    thread, self.assistant, input=payload, stream_mode="values"
                ):
                    turn.add(chunk.event, chunk.data)
        except TimeoutError:
            raise StandError(f"Ответ не завершился за {deadline:g} с") from None
        return TimedAnswer(
            text=turn.text,
            meta=turn.meta,
            first_words_s=turn.first_words_s,
            total_s=monotonic() - turn.start,
        )


async def check_health(api: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            response = await http.get(api + "/health")
            response.raise_for_status()
    except httpx.HTTPError:
        raise StandError(
            f"Стенд недоступен: {api}/health. Проверьте адрес и запуск Compose."
        ) from None
