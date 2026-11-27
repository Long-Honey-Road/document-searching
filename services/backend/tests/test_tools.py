import json
from typing import cast

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.tools import SearchRun, build_search_tools, parse_fragment_id
from regagent_common.contracts import Fragment, SearchResponse
from regagent_common.fragments import FragmentHeader, fragment_id

CONFIG: RunnableConfig = {
    "configurable": {
        "langgraph_auth_user": {"identity": "reader", "levels": ["public"]},
        "levels": ["closed_fin"],
        "user_id": "finance",
    }
}


def fragment(clause: str, text: str = "Текст пункта") -> Fragment:
    item_id = fragment_id(FragmentHeader("dib-project-activity", 3, clause, "ДИБ", "public"))
    return Fragment(
        fragment_id=item_id,
        doc_id="dib-project-activity",
        version=3,
        title="Проектная деятельность",
        clause=clause,
        area="ДИБ",
        kind="regulation",
        text=text,
        score=0.8,
    )


@pytest.mark.leak
async def test_tools_use_authenticated_levels_and_only_expose_model_inputs() -> None:
    first = fragment("2.7")
    second = fragment("2.8", "А" * 300)

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.headers["X-Service-Token"] == "test-token"
        assert json.loads(request.content) == {
            "query": "Кто назначает РП?",
            "levels": ["public"],
            "areas": ["ДИБ"],
            "top_k": None,
        }
        response = SearchResponse(
            fragments=[first, second],
            areas_found=["ДИБ"],
            areas_diverge=False,
            low_confidence=False,
        )
        return httpx.Response(200, json=response.model_dump())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KnowledgeClient("http://knowledge", "test-token", http)
        run = SearchRun()
        search, read, follow = build_search_tools(
            client, run, area_codes={"ДИБ"}, max_searches=1, result_max_chars=200
        )
        for tool in (search, read, follow):
            schema = cast(type[BaseModel], tool.tool_call_schema)
            assert set(schema.model_fields).isdisjoint(
                {"levels", "level", "user", "user_id", "sub", "groups"}
            )
        result = await search.ainvoke(
            {"query": "Кто назначает РП?", "areas": ["ДИБ", "другая"]}, config=CONFIG
        )
        assert first.fragment_id in result
        assert second.fragment_id not in result
        assert len(result) <= 200
        assert run.fragments == {first.fragment_id: first}
        assert "Лимит" in await search.ainvoke({"query": "Пункт?"}, config=CONFIG)


async def test_read_clause_404_returns_generic_text() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/clause"
        assert json.loads(request.content) == {
            "doc_id": "dib-project-activity",
            "version": 3,
            "clause": "2.7",
            "levels": ["public"],
        }
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        run = SearchRun()
        _, read, _ = build_search_tools(KnowledgeClient("http://knowledge", "token", http), run)
        message = await read.ainvoke({"fragment_id": fragment("2.7").fragment_id}, config=CONFIG)
        assert message == "Пункт не найден."
        assert not run.fragments


def test_fragment_id_matches_common_format() -> None:
    assert parse_fragment_id(fragment("2.7").fragment_id) == ("dib-project-activity", 3, "2.7")
    assert parse_fragment_id("x@1#h2") == ("x", 1, "h2")
    assert parse_fragment_id("practice:14") is None
    assert parse_fragment_id("x@bad#2.7") is None


def refs_response(
    outgoing: list[dict[str, str]], incoming: list[dict[str, str]]
) -> dict[str, list[dict[str, str]]]:
    return {"outgoing": outgoing, "incoming": incoming}


SUBCONTRACT = {
    "doc_id": "subcontract-selection",
    "title": "Регламент подбора субподрядчика",
    "clause": "3.2",
    "via": "регламентом подбора субподрядчика",
}
MARGIN = {
    "doc_id": "margin-control",
    "title": "Регламент контроля маржинальности сделок",
    "clause": "1.4",
    "via": "регламенте контроля маржинальности сделок",
}


@pytest.mark.leak
async def test_follow_refs_uses_authenticated_levels_and_lists_related_documents() -> None:
    requests: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/refs"
        assert request.headers["X-Service-Token"] == "token"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=refs_response([SUBCONTRACT], [MARGIN]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        run = SearchRun()
        tools = build_search_tools(KnowledgeClient("http://knowledge", "token", http), run)
        follow = next(tool for tool in tools if tool.name == "follow_refs")
        schema = cast(type[BaseModel], follow.tool_call_schema)
        assert set(schema.model_fields) == {"doc_id"}

        by_doc = await follow.ainvoke({"doc_id": "dib-project-activity"}, config=CONFIG)
        by_fragment = await follow.ainvoke({"doc_id": fragment("2.7").fragment_id}, config=CONFIG)

    assert requests == [{"doc_id": "dib-project-activity", "levels": ["public"]}] * 2
    assert by_doc == by_fragment
    assert "«Регламент подбора субподрядчика» (subcontract-selection)" in by_doc
    assert "п. 3.2" in by_doc
    assert "«Регламент контроля маржинальности сделок» (margin-control)" in by_doc
    assert "п. 1.4" in by_doc
    assert by_doc.index("Этот документ ссылается") < by_doc.index("На этот документ ссылаются")
    # Ссылки не текст пунктов: подтверждать ими утверждения нельзя
    assert not run.fragments


async def test_follow_refs_hides_whether_document_exists() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["doc_id"] == "missing":
            return httpx.Response(404)
        return httpx.Response(200, json=refs_response([], []))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tools = build_search_tools(KnowledgeClient("http://knowledge", "t", http), SearchRun())
        follow = next(tool for tool in tools if tool.name == "follow_refs")
        hidden = await follow.ainvoke({"doc_id": "missing"}, config=CONFIG)
        empty = await follow.ainvoke({"doc_id": "dib-project-activity"}, config=CONFIG)
    assert hidden == empty == "Связанных документов нет."


async def test_follow_refs_reports_unavailable_knowledge() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tools = build_search_tools(KnowledgeClient("http://knowledge", "t", http), SearchRun())
        follow = next(tool for tool in tools if tool.name == "follow_refs")
        assert await follow.ainvoke({"doc_id": "x"}, config=CONFIG) == "Поиск временно недоступен."


async def test_follow_refs_output_respects_size_limit() -> None:
    many = [{**SUBCONTRACT, "doc_id": f"doc-{i}", "title": "Т" * 80} for i in range(50)]

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=refs_response(many, []))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        tools = build_search_tools(
            KnowledgeClient("http://knowledge", "t", http), SearchRun(), result_max_chars=500
        )
        follow = next(tool for tool in tools if tool.name == "follow_refs")
        result = await follow.ainvoke({"doc_id": "x"}, config=CONFIG)
    assert len(result) <= 500
    assert "doc-0" in result
