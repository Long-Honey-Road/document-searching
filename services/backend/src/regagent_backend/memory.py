"""Долгая память об областях, названных или подтверждённых пользователем."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal
from weakref import WeakValueDictionary

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.store.base import BaseStore, GetOp, Item, PutOp
from pydantic import AwareDatetime, BaseModel, ConfigDict, PositiveInt, ValidationError

from regagent_backend.identity import current_identity

ALLOWED_KINDS = frozenset({"area"})
MemorySource = Literal["stated", "confirmed"]


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["area"]
    value: str
    source: MemorySource
    confirmed_at: AwareDatetime
    times_confirmed: PositiveInt

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"


# Граф и HTTP используют один Store. Сериализуем read-modify-write для одного
# пользователя, даже когда разные запросы создают собственные MemoryService.
_write_locks: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = (
    WeakValueDictionary()
)


def _write_lock(sub: str) -> asyncio.Lock:
    key = (asyncio.get_running_loop(), sub)
    lock = _write_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[key] = lock
    return lock


def _namespace(sub: str) -> tuple[str, ...]:
    return ("users", sub, "memory")


class MemoryService:
    def __init__(self, store: BaseStore, area_codes: set[str], max_active: int, ttl_days: int):
        if max_active < 1 or ttl_days < 1:
            raise ValueError("Лимит и срок памяти должны быть положительными")
        self._store = store
        self._area_codes = frozenset(area_codes)
        self._max_active = max_active
        self._ttl = timedelta(days=ttl_days)

    async def load(self, sub: str) -> list[MemoryRecord]:
        namespace = _namespace(sub)
        items = await self._store.abatch(GetOp(namespace, f"area:{v}") for v in self._area_codes)
        records = []
        for item in items:
            if not isinstance(item, Item) or item.namespace != namespace:
                continue
            try:
                record = MemoryRecord.model_validate(item.value)
            except ValidationError:
                continue
            if record.value in self._area_codes and record.key == item.key:
                records.append(record)
        return sorted(records, key=lambda r: (r.confirmed_at, r.key), reverse=True)

    async def remember(self, sub: str, kind: str, value: str, source: MemorySource) -> MemoryRecord:
        if kind not in ALLOWED_KINDS or value not in self._area_codes:
            raise ValueError("Неизвестный тип или область памяти")

        async with _write_lock(sub):
            records = await self.load(sub)
            previous = next((record for record in records if record.value == value), None)
            record = MemoryRecord(
                kind="area",
                value=value,
                source=source,
                confirmed_at=datetime.now(UTC),
                times_confirmed=previous.times_confirmed + 1 if previous else 1,
            )
            others = [old for old in records if old.key != record.key]
            namespace = _namespace(sub)
            await self._store.abatch(
                [
                    PutOp(namespace, record.key, record.model_dump(mode="json"), index=False),
                    *(PutOp(namespace, old.key, None) for old in others[self._max_active - 1 :]),
                ]
            )
            return record

    async def forget(self, sub: str, kind: str, value: str) -> bool:
        if kind not in ALLOWED_KINDS or not value.strip() or "\x00" in value:
            raise ValueError("Недопустимый ключ памяти")

        # Запись можно удалить и после исключения её области из справочника.
        namespace, key = _namespace(sub), f"{kind}:{value}"
        async with _write_lock(sub):
            if await self._store.aget(namespace, key) is None:
                return False
            await self._store.adelete(namespace, key)
            return True

    def is_stale(self, record: MemoryRecord, now: datetime) -> bool:
        return now - record.confirmed_at > self._ttl

    def render_for_prompt(self, records: list[MemoryRecord], now: datetime) -> str:
        if not records:
            return "Недавние области: нет."
        labels = []
        for record in records:
            age = max(0, (now - record.confirmed_at).days)
            status = f"дней с подтверждения: {age}"
            if self.is_stale(record, now):
                status += ", устарело"
            labels.append(f"{record.value} ({status})")
        return "Недавние области: " + "; ".join(labels)


def build_memory_tools(
    memory: MemoryService, *, remember_area: str | None = None
) -> list[BaseTool]:
    @tool
    async def remember(kind: str, value: str, config: RunnableConfig) -> str:
        """Запомни названную пользователем область по его явной просьбе."""
        if remember_area is None or value != remember_area:
            return "Для сохранения нужна явная просьба пользователя с названием области."
        try:
            await memory.remember(current_identity(config).sub, kind, value, "stated")
        except ValueError:
            return "Неизвестный тип или область памяти."
        return "Область сохранена."

    @tool
    async def forget(kind: str, value: str, config: RunnableConfig) -> str:
        """Удали сохранённую область по просьбе пользователя."""
        try:
            removed = await memory.forget(current_identity(config).sub, kind, value)
        except ValueError:
            return "Недопустимый ключ памяти."
        return "Запись удалена." if removed else "Такой записи нет."

    return [remember, forget]
