"""События backend (C3): итог хода, guard, инструменты поиска и ссылок."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.tools import SearchRun, build_search_tools

from .test_graph import CONFIG as GRAPH_CONFIG
from .test_graph import PUNKT, guard, stand
from .test_tools import CONFIG as TOOL_CONFIG

__all__ = ["stand"]

RUN = {"run_id": "run-1", "thread_id": "thread-1"}


def _with_run(config: RunnableConfig) -> RunnableConfig:
    return {"configurable": {**config["configurable"], **RUN}}


def events(capsys: pytest.CaptureFixture[str], name: str) -> list[dict[str, Any]]:
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [line for line in lines if line["event"] == name]


async def test_turn_result_records_outcome_reason_and_run(
    stand: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    model, graph, _ = stand
    model.responses = [guard(on_topic=False)]
    await graph.ainvoke(
        {"messages": [("human", "Посчитай маржу сделки.")]}, config=_with_run(GRAPH_CONFIG)
    )
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    [turn] = [line for line in lines if line["event"] == "turn.result"]
    assert (turn["service"], turn["status"]) == ("backend", "ok")
    assert (turn["outcome"], turn["reason"]) == ("refusal", "off_topic")
    assert (turn["run_id"], turn["thread_id"]) == ("run-1", "thread-1")
    assert turn["user"] == "reader"
    assert isinstance(turn["duration_ms"], int)
    [classify] = [line for line in lines if line["event"] == "guard.classify"]
    assert (classify["status"], classify["run_id"]) == ("ok", "run-1")
    assert "Посчитай" not in out, "текст вопроса не попадает в события"


async def test_turn_result_for_answer_counts_searches_and_sources(
    stand: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    model, graph, _ = stand
    model.responses = [
        guard("ДИБ"),
        {"content": f"Координатор назначает РП [f:{PUNKT.fragment_id}]."},
    ]
    await graph.ainvoke(
        {"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=_with_run(GRAPH_CONFIG)
    )
    [turn] = events(capsys, "turn.result")
    assert (turn["outcome"], turn["area"], turn["area_source"]) == ("answer", "ДИБ", "question")
    assert (turn["searches"], turn["fragments"], turn["sources"]) == (1, 1, 1)
    assert turn["search_unavailable"] == 0
    assert "reason" not in turn or turn["reason"] is None


async def test_rate_limit_is_a_turn_result(stand: Any, capsys: pytest.CaptureFixture[str]) -> None:
    model, graph, _ = stand
    model.responses = [guard(on_topic=False)] * 20
    for _ in range(21):
        await graph.ainvoke({"messages": [("human", "вопрос")]}, config=_with_run(GRAPH_CONFIG))
    turns = events(capsys, "turn.result")
    assert turns[-1]["reason"] == "rate_limit"


async def _tool(name: str, handle: Any) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    tools = build_search_tools(
        KnowledgeClient("http://knowledge", "t", http), SearchRun(), area_codes={"ДИБ"}
    )
    return next(tool for tool in tools if tool.name == name), http


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (
            httpx.Response(
                200,
                json={
                    "fragments": [PUNKT.model_dump()],
                    "areas_found": ["ДИБ"],
                    "areas_diverge": False,
                    "low_confidence": False,
                },
            ),
            "ok",
        ),
        (
            httpx.Response(
                200,
                json={
                    "fragments": [],
                    "areas_found": [],
                    "areas_diverge": False,
                    "low_confidence": True,
                },
            ),
            "empty",
        ),
        (httpx.Response(503), "unavailable"),
    ],
)
async def test_search_tool_event_distinguishes_empty_from_unavailable(
    capsys: pytest.CaptureFixture[str], response: httpx.Response, status: str
) -> None:
    tool, http = await _tool("search_docs", lambda request: response)
    async with http:
        await tool.ainvoke({"query": "секретный текст запроса"}, config=_with_run(TOOL_CONFIG))
    [event] = events(capsys, "search.tool")
    assert (event["status"], event["run_id"]) == (status, "run-1")
    assert "секретный" not in json.dumps(event, ensure_ascii=False)


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(200, json={"outgoing": [], "incoming": []}), "empty"),
        (httpx.Response(404), "not_found"),
        (httpx.Response(503), "unavailable"),
    ],
)
async def test_refs_tool_event_distinguishes_empty_from_unavailable(
    capsys: pytest.CaptureFixture[str], response: httpx.Response, status: str
) -> None:
    tool, http = await _tool("follow_refs", lambda request: response)
    async with http:
        await tool.ainvoke({"doc_id": "dib"}, config=_with_run(TOOL_CONFIG))
    [event] = events(capsys, "refs.tool")
    assert (event["status"], event["doc_id"]) == (status, "dib")
