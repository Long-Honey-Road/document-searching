"""Память об области по прямым указаниям пользователя (C1, D-55).

Сигнал — область, названная пользователем в вопросе или в ответе на уточнение. Памяти нет —
область запоминается со 2-го сигнала; в памяти другая — меняется после 3 сигналов подряд.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore
from regagent_backend.graph import build_graph
from regagent_backend.memory import MemoryService

from .test_graph import CONFIG, PUNKT, guard, meta, search, stand_parts

NOW = datetime(2026, 11, 28, 10, tzinfo=UTC)


@pytest.fixture
def memory() -> MemoryService:
    return MemoryService(
        InMemoryStore(),
        {"ДИБ", "ДБРЦТ", "общий"},
        max_active=3,
        ttl_days=14,
        signals_to_remember=2,
        signals_to_switch=3,
        signal_window_days=14,
    )


async def _current(memory: MemoryService, sub: str = "u") -> str | None:
    records = await memory.load(sub)
    fresh = [record for record in records if not memory.is_stale(record, NOW)]
    return fresh[0].value if fresh else None


async def test_second_signal_remembers_area(memory: MemoryService) -> None:
    first = await memory.observe("u", "run-1", "ДИБ", NOW)
    assert (first.status, first.count, first.needed) == ("pending", 1, 2)
    assert await _current(memory) is None
    second = await memory.observe("u", "run-2", "ДИБ", NOW)
    assert second.status == "remembered"
    assert await _current(memory) == "ДИБ"


async def test_same_run_counts_once(memory: MemoryService) -> None:
    await memory.observe("u", "run-1", "ДИБ", NOW)
    again = await memory.observe("u", "run-1", "ДИБ", NOW)
    assert again.status == "duplicate"
    assert await _current(memory) is None


async def test_signal_for_current_area_confirms_it(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    later = NOW + timedelta(days=10)
    decision = await memory.observe("u", "r3", "ДИБ", later)
    assert decision.status == "confirmed"
    [record] = await memory.load("u")
    assert record.times_confirmed == 2
    assert not memory.is_stale(record, later + timedelta(days=13))


async def test_three_signals_in_a_row_switch_area(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    one = await memory.observe("u", "r3", "ДБРЦТ", NOW)
    two = await memory.observe("u", "r4", "ДБРЦТ", NOW)
    assert [(d.status, d.count, d.needed) for d in (one, two)] == [
        ("pending", 1, 3),
        ("pending", 2, 3),
    ]
    assert await _current(memory) == "ДИБ"
    three = await memory.observe("u", "r5", "ДБРЦТ", NOW)
    assert three.status == "switched"
    assert await _current(memory) == "ДБРЦТ"


async def test_current_area_between_breaks_the_streak(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    await memory.observe("u", "r3", "ДБРЦТ", NOW)
    await memory.observe("u", "r4", "ДБРЦТ", NOW)
    await memory.observe("u", "r5", "ДИБ", NOW)
    decision = await memory.observe("u", "r6", "ДБРЦТ", NOW)
    assert (decision.status, decision.count) == ("pending", 1)
    assert await _current(memory) == "ДИБ"


async def test_old_signals_leave_the_window(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    decision = await memory.observe("u", "r2", "ДИБ", NOW + timedelta(days=15))
    assert (decision.status, decision.count) == ("pending", 1)


async def test_stale_memory_is_treated_as_absent(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    later = NOW + timedelta(days=20)
    assert await memory.purge_stale("u", later) == ["ДИБ"]
    assert await memory.load("u") == []
    first = await memory.observe("u", "r3", "ДБРЦТ", later)
    assert (first.status, first.needed) == ("pending", 2), "без памяти — правило первой записи"


@pytest.mark.leak
async def test_signals_are_per_account(memory: MemoryService) -> None:
    await memory.observe("a", "r1", "ДИБ", NOW)
    decision = await memory.observe("b", "r2", "ДИБ", NOW)
    assert (decision.status, decision.count) == ("pending", 1)
    assert await memory.area_signals("b") == [("ДИБ", NOW)]


async def test_unknown_area_is_rejected(memory: MemoryService) -> None:
    with pytest.raises(ValueError):
        await memory.observe("u", "r1", "Склад", NOW)


async def test_progress_summary_for_the_user(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    await memory.observe("u", "r3", "ДБРЦТ", NOW)
    progress = await memory.progress("u", NOW)
    assert progress.current == "ДИБ"
    assert (progress.pending_area, progress.pending_count, progress.pending_needed) == (
        "ДБРЦТ",
        1,
        3,
    )


async def test_forgetting_signals_resets_progress(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    assert await memory.forget("u", "signals", "area")
    decision = await memory.observe("u", "r2", "ДИБ", NOW)
    assert (decision.status, decision.count) == ("pending", 1)


# --- в графе ----------------------------------------------------------------------

ANSWER = {"content": f"Координатор назначает РП [f:{PUNKT.fragment_id}]."}


def _config(run: str) -> Any:
    return {"configurable": {**CONFIG["configurable"], "run_id": run, "thread_id": "t"}}


async def _ask(graph: Any, model: Any, area: str | None, run: str) -> Any:
    model.responses = [guard(area) if area else guard(), search(), ANSWER]
    state = await graph.ainvoke({"messages": [("human", "Кто назначает РП?")]}, config=_config(run))
    return meta(state["messages"][-1])


async def test_graph_switches_area_after_three_explicit_turns_in_a_row() -> None:
    async with stand_parts() as (model, deps, _):
        graph: Any = build_graph(deps, store=InMemoryStore())
        for run in ("r1", "r2"):
            await _ask(graph, model, "ДИБ", run)
        assert (await _ask(graph, model, None, "r3")).area_used == "ДИБ"
        for run in ("r4", "r5"):
            answered = await _ask(graph, model, "ДБРЦТ", run)
            assert (answered.area_used, answered.area_source) == ("ДБРЦТ", "question")
        still = await _ask(graph, model, None, "r6")
        assert (still.area_used, still.area_source) == ("ДИБ", "memory")
        await _ask(graph, model, "ДБРЦТ", "r7")
        await _ask(graph, model, "ДБРЦТ", "r8")
        await _ask(graph, model, "ДБРЦТ", "r9")
        switched = await _ask(graph, model, None, "r10")
        assert (switched.area_used, switched.area_source) == ("ДБРЦТ", "memory")


async def test_graph_counts_a_redelivered_turn_once() -> None:
    async with stand_parts() as (model, deps, _):
        graph: Any = build_graph(deps, store=InMemoryStore())
        await _ask(graph, model, "ДИБ", "same-run")
        await _ask(graph, model, "ДИБ", "same-run")
        model.responses = [guard(), {"content": "УТОЧНЕНИЕ: Для какой области нужен ответ?"}]
        state = await graph.ainvoke(
            {"messages": [("human", "Кто назначает РП?")]}, config=_config("next")
        )
        assert meta(state["messages"][-1]).outcome == "ask_area", "один сигнал — ещё не память"


async def test_graph_asks_once_when_memory_is_stale_and_forgets_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryStore()
    async with stand_parts() as (model, deps, lookups):
        graph: Any = build_graph(deps, store=store)
        memory = MemoryService(store, {"ДИБ", "ДБРЦТ", "общий"}, max_active=3, ttl_days=14)
        long_ago = datetime.now(UTC) - timedelta(days=30)
        await memory.observe("reader", "old-1", "ДИБ", long_ago)
        await memory.observe("reader", "old-2", "ДИБ", long_ago)
        model.responses = [guard()]
        state = await graph.ainvoke(
            {"messages": [("human", "Кто назначает РП?")]}, config=_config("now-1")
        )
        assert meta(state["messages"][-1]).outcome == "ask_area"
        assert not lookups, "по устаревшей памяти не ищем"
        assert await memory.load("reader") == []
        out = capsys.readouterr().out
        assert '"event":"memory.expired"' in out and "ДИБ" in out


async def test_forgetting_an_area_drops_its_signals(memory: MemoryService) -> None:
    await memory.observe("u", "r1", "ДИБ", NOW)
    await memory.observe("u", "r2", "ДИБ", NOW)
    await memory.observe("u", "r3", "ДБРЦТ", NOW)
    assert await memory.forget("u", "area", "ДИБ")
    assert await memory.area_signals("u") == [("ДБРЦТ", NOW)]
    decision = await memory.observe("u", "r4", "ДИБ", NOW)
    assert (decision.status, decision.count) == ("pending", 1)
