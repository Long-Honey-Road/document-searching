"""Итог хода для аналитики ОКК (C2): точка вызова в графе, исходы, изоляция сбоев."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore
from regagent_backend.graph import build_graph
from regagent_common.contracts import TurnReport

from .test_graph import CONFIG, PUNKT, guard, meta, stand_parts

RUN_CONFIG = {"configurable": {**CONFIG["configurable"], "run_id": "run-7", "thread_id": "t-7"}}


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.reports: list[TurnReport] = []
        self.identities: list[object] = []
        self.fail = fail

    async def record(self, report: TurnReport, identity: object) -> None:
        self.identities.append(identity)
        if self.fail:
            raise RuntimeError("analytics down")
        self.reports.append(report)


@pytest.fixture
async def stand() -> Any:
    async with stand_parts() as (model, deps, lookups):
        yield model, deps, lookups


def _with_sink(deps: Any, sink: Sink) -> Any:
    return build_graph(replace(deps, turn_sink=sink), store=InMemoryStore())


async def test_answer_turn_reports_question_area_fragments_and_citations(stand: Any) -> None:
    model, deps, _ = stand
    sink = Sink()
    graph = _with_sink(deps, sink)
    model.responses = [
        guard("ДИБ"),
        {"content": f"Координатор назначает РП [f:{PUNKT.fragment_id}]."},
    ]
    await graph.ainvoke({"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=RUN_CONFIG)
    [report] = sink.reports
    assert (report.run_id, report.thread_id, report.user_sub) == ("run-7", "t-7", "reader")
    assert (report.outcome, report.reason) == ("answered", None)
    assert report.question == "Кто назначает РП в ДИБ?"
    assert (report.area, report.area_source) == ("ДИБ", "question")
    assert report.fragments == [PUNKT.fragment_id] and report.cited == [PUNKT.fragment_id]
    assert (report.practice_used, report.searches) == (False, 1)
    assert sink.identities[0].levels == frozenset({"public"})


async def test_empty_search_is_reported(stand: Any) -> None:
    model, deps, _ = stand
    model.fragments = []
    sink = Sink()
    graph = _with_sink(deps, sink)
    model.responses = [guard("ДИБ"), {"content": "В регламентах этого нет."}]
    await graph.ainvoke({"messages": [("human", "Где брать пропуск?")]}, config=RUN_CONFIG)
    [report] = sink.reports
    assert (report.outcome, report.reason) == ("empty_search", "empty_search")
    assert report.fragments == []


async def test_refusal_is_reported(stand: Any) -> None:
    model, deps, _ = stand
    sink = Sink()
    graph = _with_sink(deps, sink)
    model.responses = [guard(on_topic=False)]
    await graph.ainvoke({"messages": [("human", "Посчитай маржу.")]}, config=RUN_CONFIG)
    [report] = sink.reports
    assert (report.outcome, report.reason) == ("refused", "off_topic")


async def test_failed_analytics_does_not_change_the_answer(
    stand: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    model, deps, _ = stand
    graph = _with_sink(deps, Sink(fail=True))
    model.responses = [
        guard("ДИБ"),
        {"content": f"Координатор назначает РП [f:{PUNKT.fragment_id}]."},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=RUN_CONFIG
    )
    assert meta(state["messages"][-1]).outcome == "answer"
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    [failure] = [line for line in lines if line["event"] == "turn.report"]
    assert (failure["status"], failure["error_code"], failure["run_id"]) == (
        "error",
        "RuntimeError",
        "run-7",
    )


@pytest.mark.parametrize(
    ("reason", "error_code", "unavailable", "outcome"),
    [
        ("generation_error", "APITimeoutError", 0, "model_timeout"),
        ("guard_error", "APITimeoutError", 0, "model_timeout"),
        ("generation_error", "APIConnectionError", 0, "error"),
        ("citations_rejected", None, 0, "unsupported"),
        ("comparison_rejected", None, 0, "unsupported"),
        ("low_confidence", None, 0, "unsupported"),
        ("areas_diverge", None, 0, "ask_area"),
        ("memory_stale", None, 0, "ask_area"),
        ("generation_error", "APIConnectionError", 2, "search_unavailable"),
        ("empty_search", None, 1, "search_unavailable"),
    ],
)
def test_outcome_codes(reason: str, error_code: str | None, unavailable: int, outcome: str) -> None:
    from regagent_backend.graph import report_outcome

    agent_outcome = {
        "model_timeout": "fallback",
        "error": "fallback",
        "unsupported": "fallback",
        "ask_area": "ask_area",
        "search_unavailable": "not_found",
    }[outcome]
    if reason == "low_confidence":
        agent_outcome = "not_found"
    assert report_outcome(agent_outcome, reason, error_code, unavailable, fragments=0) == outcome
