"""Практика в ответе (C2, A3): clarifies дополняет пункт, overrides заменяет его, и ответ
по заменённому пункту обязан процитировать практику."""

from __future__ import annotations

import json
from typing import Any

import httpx
from langgraph.store.memory import InMemoryStore
from regagent_backend.citations import missing_overrides
from regagent_backend.graph import build_graph
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.tools import SearchRun, build_search_tools
from regagent_common.contracts import Fragment, PracticeBinding, PracticeSource, SearchResponse

from .test_graph import CONFIG, PUNKT, guard, meta, stand_parts
from .test_tools import CONFIG as TOOL_CONFIG


def practice(mode: str, practice_id: str = "taxi-night") -> Fragment:
    return Fragment(
        fragment_id=f"practice:{practice_id}",
        doc_id=f"practice-{practice_id}",
        version=None,
        title="Пояснение ОКК",
        clause="practice",
        area="ДИБ",
        kind="practice",
        text="РП назначает заместитель директора, если координатор в отпуске.",
        score=0.8,
        practice=PracticeSource(
            mode=mode,
            binds=[PracticeBinding(doc_id=PUNKT.doc_id, version=1, clause=PUNKT.clause)],
        ),
    )


def test_overrides_practice_must_be_cited_with_its_clause() -> None:
    override = practice("overrides")
    fragments = {f.fragment_id: f for f in (PUNKT, override)}
    only_clause = f"Координатор назначает РП [f:{PUNKT.fragment_id}]."
    assert missing_overrides(only_clause, fragments) == [override.fragment_id]
    both = only_clause + f" Но по практике ОКК — заместитель [f:{override.fragment_id}]."
    assert missing_overrides(both, fragments) == []


def test_clarifying_practice_is_optional() -> None:
    fragments = {f.fragment_id: f for f in (PUNKT, practice("clarifies"))}
    assert missing_overrides(f"Координатор [f:{PUNKT.fragment_id}].", fragments) == []


def test_override_of_another_clause_does_not_apply() -> None:
    other = practice("overrides").model_copy(
        update={
            "practice": PracticeSource(
                mode="overrides",
                binds=[PracticeBinding(doc_id=PUNKT.doc_id, version=1, clause="3.1")],
            )
        }
    )
    fragments = {f.fragment_id: f for f in (PUNKT, other)}
    assert missing_overrides(f"Координатор [f:{PUNKT.fragment_id}].", fragments) == []


async def test_search_tool_shows_practice_mode_and_bound_clause() -> None:
    response = SearchResponse(
        fragments=[PUNKT, practice("overrides")],
        areas_found=["ДИБ"],
        areas_diverge=False,
        low_confidence=False,
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response.model_dump()))
    )
    async with http:
        [search, *_] = build_search_tools(KnowledgeClient("http://k", "t", http), SearchRun())
        text = await search.ainvoke({"query": "кто назначает РП"}, config=TOOL_CONFIG)
    assert "практика ОКК, заменяет п. 2.7 документа dib-project-activity (ред. 1)" in text
    clarify = response.model_copy(update={"fragments": [practice("clarifies")]})
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=clarify.model_dump()))
    )
    async with http:
        [search, *_] = build_search_tools(KnowledgeClient("http://k", "t", http), SearchRun())
        text = await search.ainvoke({"query": "кто назначает РП"}, config=TOOL_CONFIG)
    assert "практика ОКК, уточняет п. 2.7" in text


async def test_answer_ignoring_override_is_rewritten_once() -> None:
    override = practice("overrides")
    async with stand_parts() as (model, deps, _):
        model.fragments = [PUNKT, override]
        graph: Any = build_graph(deps, store=InMemoryStore())
        model.responses = [
            guard("ДИБ"),
            {"content": f"Координатор назначает РП [f:{PUNKT.fragment_id}]."},
            {
                "content": f"По регламенту — координатор [f:{PUNKT.fragment_id}], "
                f"но по практике ОКК — заместитель директора [f:{override.fragment_id}]."
            },
        ]
        state = await graph.ainvoke(
            {"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=CONFIG
        )
        answer = meta(state["messages"][-1])
        assert answer.outcome == "answer"
        assert [s.kind for s in answer.sources] == ["regulation", "practice"]
        retry = json.dumps(model.requests[-1]["messages"], ensure_ascii=False)
        assert "заменяет" in retry and override.fragment_id in retry
