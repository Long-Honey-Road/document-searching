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
from regagent_backend.settings import BackendSettings

ALLOWED_KINDS = frozenset({"area"})
MemorySource = Literal["stated", "confirmed"]
SIGNALS_KEY = "signals:area"
MAX_SIGNALS = 20
AreaStatus = Literal["duplicate", "pending", "remembered", "confirmed", "switched"]


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


class AreaSignal(BaseModel):
    """Прямое указание области пользователем в одном ходе (вопрос или ответ на уточнение)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    area: str
    at: AwareDatetime


class AreaDecision(BaseModel):
    status: AreaStatus
    area: str
    count: int
    needed: int


class AreaProgress(BaseModel):
    current: str | None
    current_confirmed_at: AwareDatetime | None
    days_until_stale: int | None
    pending_area: str | None
    pending_count: int
    pending_needed: int


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
    def __init__(
        self,
        store: BaseStore,
        area_codes: set[str],
        max_active: int,
        ttl_days: int,
        *,
        signals_to_remember: int = 2,
        signals_to_switch: int = 3,
        signal_window_days: int = 14,
    ):
        if (
            min(max_active, ttl_days, signals_to_remember, signals_to_switch, signal_window_days)
            < 1
        ):
            raise ValueError("Лимиты и сроки памяти должны быть положительными")
        self._store = store
        self._area_codes = frozenset(area_codes)
        self._max_active = max_active
        self._ttl = timedelta(days=ttl_days)
        self._to_remember = signals_to_remember
        self._to_switch = signals_to_switch
        self._window = timedelta(days=signal_window_days)

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
            return await self._remember(sub, value, source, datetime.now(UTC))

    async def _remember(
        self, sub: str, value: str, source: MemorySource, now: datetime
    ) -> MemoryRecord:
        records = await self.load(sub)
        previous = next((record for record in records if record.value == value), None)
        record = MemoryRecord(
            kind="area",
            value=value,
            source=source,
            confirmed_at=now,
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

    async def _signals(self, sub: str) -> list[AreaSignal]:
        item = await self._store.aget(_namespace(sub), SIGNALS_KEY)
        raw = item.value.get("history") if item is not None else None
        signals = []
        for value in raw if isinstance(raw, list) else []:
            try:
                signal = AreaSignal.model_validate(value)
            except ValidationError:
                continue
            if signal.area in self._area_codes:
                signals.append(signal)
        return signals

    async def area_signals(self, sub: str) -> list[tuple[str, datetime]]:
        return [(signal.area, signal.at) for signal in await self._signals(sub)]

    def _current(self, records: list[MemoryRecord], now: datetime) -> MemoryRecord | None:
        return next((record for record in records if not self.is_stale(record, now)), None)

    def _streak(self, signals: list[AreaSignal], area: str) -> int:
        count = 0
        for signal in reversed(signals):
            if signal.area != area:
                break
            count += 1
        return count

    async def observe(
        self, sub: str, run_id: str, area: str, now: datetime | None = None
    ) -> AreaDecision:
        """Учесть прямое указание области в ходе ``run_id`` (D-55).

        Памяти нет — область запоминается, набрав ``signals_to_remember`` сигналов в окне.
        В памяти другая — меняется после ``signals_to_switch`` сигналов подряд. Сигнал для
        текущей области подтверждает её. Повтор того же хода не считается."""
        if area not in self._area_codes:
            raise ValueError("Неизвестная область памяти")
        now = now or datetime.now(UTC)
        async with _write_lock(sub):
            signals = [s for s in await self._signals(sub) if now - s.at <= self._window]
            records = await self.load(sub)
            current = self._current(records, now)
            if any(signal.run_id == run_id for signal in signals):
                needed = self._to_remember if current is None else self._to_switch
                return AreaDecision(status="duplicate", area=area, count=0, needed=needed)
            signals = [*signals, AreaSignal(run_id=run_id, area=area, at=now)][-MAX_SIGNALS:]
            await self._store.aput(
                _namespace(sub),
                SIGNALS_KEY,
                {"history": [signal.model_dump(mode="json") for signal in signals]},
                index=False,
            )
            if current is not None and current.value == area:
                await self._remember(sub, area, "confirmed", now)
                return AreaDecision(status="confirmed", area=area, count=1, needed=1)
            if current is None:
                count = sum(signal.area == area for signal in signals)
                if count >= self._to_remember:
                    await self._remember(sub, area, "stated", now)
                    return AreaDecision(
                        status="remembered", area=area, count=count, needed=self._to_remember
                    )
                return AreaDecision(
                    status="pending", area=area, count=count, needed=self._to_remember
                )
            streak = self._streak(signals, area)
            if streak >= self._to_switch:
                # Память меняется: прежняя область больше не текущая
                await self._store.adelete(_namespace(sub), current.key)
                await self._remember(sub, area, "stated", now)
                return AreaDecision(
                    status="switched", area=area, count=streak, needed=self._to_switch
                )
            return AreaDecision(status="pending", area=area, count=streak, needed=self._to_switch)

    async def purge_stale(self, sub: str, now: datetime | None = None) -> list[str]:
        """Устаревшая память для демо равна удалённой: удалить её записи."""
        now = now or datetime.now(UTC)
        async with _write_lock(sub):
            stale = [record for record in await self.load(sub) if self.is_stale(record, now)]
            if stale:
                namespace = _namespace(sub)
                await self._store.abatch(PutOp(namespace, record.key, None) for record in stale)
            return [record.value for record in stale]

    async def progress(self, sub: str, now: datetime | None = None) -> AreaProgress:
        """Что показать пользователю: текущая область и счёт до записи или смены."""
        now = now or datetime.now(UTC)
        records = await self.load(sub)
        current = self._current(records, now)
        signals = [s for s in await self._signals(sub) if now - s.at <= self._window]
        pending_area, count, needed = None, 0, 0
        if current is None and signals:
            pending_area = signals[-1].area
            count = sum(signal.area == pending_area for signal in signals)
            needed = self._to_remember
        elif current is not None and signals and signals[-1].area != current.value:
            pending_area = signals[-1].area
            count, needed = self._streak(signals, pending_area), self._to_switch
        days = None
        if current is not None:
            days = max(0, (current.confirmed_at + self._ttl - now).days)
        return AreaProgress(
            current=current.value if current else None,
            current_confirmed_at=current.confirmed_at if current else None,
            days_until_stale=days,
            pending_area=pending_area,
            pending_count=count,
            pending_needed=needed,
        )

    async def forget(self, sub: str, kind: str, value: str) -> bool:
        is_signals = f"{kind}:{value}" == SIGNALS_KEY
        if (kind not in ALLOWED_KINDS and not is_signals) or not value.strip() or "\x00" in value:
            raise ValueError("Недопустимый ключ памяти")

        # Запись можно удалить и после исключения её области из справочника.
        namespace, key = _namespace(sub), f"{kind}:{value}"
        async with _write_lock(sub):
            if kind in ALLOWED_KINDS:
                # Забытая область не должна вернуться в память по старым сигналам
                signals = await self._signals(sub)
                kept = [signal for signal in signals if signal.area != value]
                if len(kept) != len(signals):
                    await self._store.aput(
                        namespace,
                        SIGNALS_KEY,
                        {"history": [signal.model_dump(mode="json") for signal in kept]},
                        index=False,
                    )
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


def memory_service(store: BaseStore, settings: BackendSettings) -> MemoryService:
    """Сервис памяти с порогами из настроек: один для графа и маршрутов ``/me/memory``."""
    return MemoryService(
        store,
        set(settings.AREA_CODES),
        settings.MEMORY_MAX_ACTIVE_AREAS,
        settings.MEMORY_AREA_TTL_DAYS,
        signals_to_remember=settings.MEMORY_AREA_SIGNALS_TO_REMEMBER,
        signals_to_switch=settings.MEMORY_AREA_SIGNALS_TO_SWITCH,
        signal_window_days=settings.MEMORY_AREA_SIGNAL_WINDOW_DAYS,
    )
