import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr
from regagent_backend.graph import AgentDeps, build_graph
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.settings import BackendSettings
from regagent_backend.usage import UsageCallback
from regagent_common.contracts import AnswerMeta, ClauseResponse, Fragment, SearchResponse

CONFIG: RunnableConfig = {
    "configurable": {
        "langgraph_auth_user": {"identity": "reader", "levels": ["public"]},
        "user_id": "finance",
        "levels": ["closed_fin"],
    }
}
PUNKT = Fragment(
    fragment_id="dib-project-activity@1#2.7",
    doc_id="dib-project-activity",
    version=1,
    title="Проектная деятельность ДИБ",
    clause="2.7",
    area="ДИБ",
    kind="regulation",
    text="Координатор назначает РП в течение одного рабочего дня.",
    score=0.9,
)


class Model:
    def __init__(self) -> None:
        self.responses: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        self.fragments = [PUNKT]
        self.clauses: list[Fragment] = []
        self.search_status = 200

    async def request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        message = {"role": "assistant", **self.responses.pop(0)}
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "created": 1,
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                    }
                ],
            },
        )


def guard(*areas: str, on_topic: bool = True) -> dict[str, str]:
    return {
        "content": json.dumps(
            {
                "on_topic": on_topic,
                "injection": False,
                "partial_topic": None,
                "explicit_areas": list(areas),
                "search_queries": [],
                "memory_action": "none",
                "qc_action": "none",
                "missing_detail": None,
                "area_only": False,
            }
        )
    }


def search(number: int = 1) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"search-{number}",
                "type": "function",
                "function": {
                    "name": "search_docs",
                    "arguments": json.dumps(
                        {"query": "Назначение руководителя проекта", "areas": []}
                    ),
                },
            }
        ],
    }


@asynccontextmanager
async def stand_parts() -> AsyncIterator[tuple[Model, AgentDeps, list[dict[str, Any]]]]:
    """Подставные модель и knowledge и зависимости агента; граф собирает вызывающий."""
    model = Model()
    lookups: list[dict[str, Any]] = []

    async def knowledge(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)
        assert params["levels"] == ["public"]
        lookups.append(params)
        if request.url.path == "/clause":
            fragment = next(
                (
                    item
                    for item in model.clauses
                    if (item.doc_id, item.version, item.clause)
                    == (params["doc_id"], params["version"], params["clause"])
                ),
                None,
            )
            if fragment is None:
                return httpx.Response(404)
            return httpx.Response(
                200, json=ClauseResponse(fragment=fragment, neighbors=[]).model_dump()
            )
        if model.search_status != 200:
            return httpx.Response(model.search_status)
        response = SearchResponse(
            fragments=model.fragments,
            areas_found=["ДИБ"] if params["areas"] else ["ДИБ", "ДБРЦТ"],
            areas_diverge=not params["areas"],
            low_confidence=False,
        )
        return httpx.Response(200, json=response.model_dump())

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(model.request)) as transport,
        httpx.AsyncClient(transport=httpx.MockTransport(knowledge)) as knowledge_http,
    ):
        settings = BackendSettings(
            _env_file=None,
            AREA_CODES=["ДИБ", "ДБРЦТ", "общий"],
            GUARD_MODE="json_schema",
            RATE_LIMIT_PER_USER_PER_MIN=20,
            KEYCLOAK_ISSUER="issuer",
            KEYCLOAK_AUDIENCE="aud",
            KEYCLOAK_JWKS_URL="https://keys",
            KNOWLEDGE_URL="http://knowledge",
            KNOWLEDGE_INTERNAL_TOKEN=SecretStr("internal"),
        )
        chat = ChatOpenAI(
            model="fake",
            base_url="http://llm/v1",
            api_key=SecretStr("fake"),
            http_async_client=transport,
            disable_streaming=True,
            callbacks=[UsageCallback("agent")],
        )
        deps = AgentDeps(
            settings,
            chat,
            chat,
            KnowledgeClient("http://knowledge", "internal", knowledge_http),
        )
        yield model, deps, lookups


@pytest.fixture
async def stand() -> AsyncIterator[tuple[Model, Any, list[dict[str, Any]]]]:
    async with stand_parts() as (model, deps, lookups):
        yield model, build_graph(deps, store=InMemoryStore()), lookups


def meta(message: AIMessage) -> AnswerMeta:
    return AnswerMeta.model_validate(message.additional_kwargs["answer_meta"])


@pytest.mark.leak
async def test_clarification_semantic_area_memory_and_tool_allowlist(stand: Any) -> None:
    model, graph, lookups = stand
    model.responses = [
        guard(),
        search(),
        {"content": "Координатор департамента [f:dib-project-activity@1#2.7]."},
    ]
    first = await graph.ainvoke(
        {"messages": [("human", "Кто назначает РП на проект?")]}, config=CONFIG
    )
    assert meta(first["messages"][-1]).outcome == "ask_area"
    assert lookups[-1]["areas"] == []

    answer = {
        "content": "<think>внутренний текст</think>Координатор [f:dib-project-activity@1#2.7]."
    }
    model.responses = [guard("ДИБ"), search(), answer]
    states = [
        event
        async for event in graph.astream(
            {"messages": [*first["messages"], ("human", "В дирекции ИБ. И какой срок?")]},
            config=CONFIG,
            stream_mode=["values", "messages"],
            subgraphs=True,
        )
    ]
    values = [payload for ns, mode, payload in states if not ns and mode == "values"]
    second = values[-1]["messages"][-1]
    assert meta(second).area_source == "asked"
    assert meta(second).area_used == "ДИБ"
    assert meta(second).sources[0].fragment_id == PUNKT.fragment_id
    assert "внутренний текст" not in repr(states)
    assert PUNKT.text not in repr(states)
    assert lookups[-1]["areas"] == ["ДИБ"]
    assert all(
        {tool["function"]["name"] for tool in request.get("tools", [])}
        <= {"search_docs", "read_clause", "follow_refs"}
        for request in model.requests
    )
    prompt = model.requests[-1]["messages"][0]["content"]
    assert "каждое утверждение" in prompt.lower()
    assert "closed_fin" not in prompt

    # Один сигнал (ответ на уточнение) ещё не память: нужен второй (D-55)
    model.responses = [guard(), {"content": "УТОЧНЕНИЕ: Для какой области нужен ответ?"}]
    unscoped = await graph.ainvoke({"messages": [("human", "Кто назначает РП?")]}, config=CONFIG)
    assert meta(unscoped["messages"][-1]).outcome == "ask_area"
    model.responses = [guard("ДИБ"), search(), answer]
    await graph.ainvoke({"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=CONFIG)

    model.responses = [guard(), search(), answer]
    remembered = await graph.ainvoke({"messages": [("human", "Кто назначает РП?")]}, config=CONFIG)
    assert meta(remembered["messages"][-1]).area_source == "memory"
    assert lookups[-1]["areas"] == ["ДИБ"]
    assert "недавние области:" in model.requests[-1]["messages"][0]["content"].lower()


async def test_bad_citation_retries_once_then_fallback(stand: Any, capsys: Any) -> None:
    model, graph, _ = stand
    wrong = {"content": "Срок два дня [f:x@1#1]."}
    model.responses = [guard("ДИБ"), search(), wrong, wrong]
    state = await graph.ainvoke({"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=CONFIG)
    reply = state["messages"][-1]
    assert meta(reply).outcome == "fallback"
    assert len(model.requests) == 4
    assert len(state["messages"]) == 2
    assert "два дня" not in reply.content
    checks = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"event":"answer.citations_check"' in line
    ]
    assert len(checks) == 2
    assert all(check["status"] == "rejected" and check["unknown_count"] == 1 for check in checks)
    assert all("два дня" not in json.dumps(check) for check in checks)


async def test_existing_cited_clause_outside_search_top_k_is_checked_and_answered(
    stand: Any,
) -> None:
    model, graph, lookups = stand
    model.fragments = [
        PUNKT.model_copy(
            update={
                "fragment_id": "dib-project-activity@1#2",
                "clause": "2",
                "text": "Раздел 2. Назначение руководителя проекта.",
            }
        )
    ]
    model.clauses = [PUNKT]
    model.responses = [
        guard("ДИБ"),
        {
            "content": (
                "Координатор назначает РП за один рабочий день [f:dib-project-activity@1#2.7]."
            )
        },
        {"content": '{"outcome":"answer","question":null}'},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Кто назначает РП в ДИБ и за какой срок?")]}, config=CONFIG
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "answer"
    assert meta(reply).sources[0].fragment_id == PUNKT.fragment_id
    assert any("doc_id" in lookup and lookup["clause"] == "2.7" for lookup in lookups)
    assert PUNKT.text in model.requests[-1]["messages"][1]["content"]


async def test_unavailable_clause_cannot_become_a_source(stand: Any) -> None:
    model, graph, lookups = stand
    model.fragments = [
        PUNKT.model_copy(
            update={
                "fragment_id": "dib-project-activity@1#2",
                "clause": "2",
            }
        )
    ]
    draft = {"content": "Срок два дня [f:dib-project-activity@1#2.8]."}
    model.responses = [guard("ДИБ"), draft, draft]
    state = await graph.ainvoke(
        {"messages": [("human", "Какой срок назначения РП в ДИБ?")]}, config=CONFIG
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "fallback"
    assert not meta(reply).sources
    assert len([lookup for lookup in lookups if lookup.get("clause") == "2.8"]) == 2


async def test_loaded_clause_still_needs_evidence_for_the_claim(stand: Any) -> None:
    model, graph, _ = stand
    model.fragments = [
        PUNKT.model_copy(
            update={
                "fragment_id": "dib-project-activity@1#2",
                "clause": "2",
            }
        )
    ]
    model.clauses = [PUNKT]
    model.responses = [
        guard("ДИБ"),
        {"content": "Срок два дня [f:dib-project-activity@1#2.7]."},
        {"content": '{"outcome":"not_found","question":null}'},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Какой срок назначения РП в ДИБ?")]}, config=CONFIG
    )
    assert meta(state["messages"][-1]).outcome == "not_found"
    assert not meta(state["messages"][-1]).sources


async def test_mixed_no_answer_marker_is_not_published_as_answer(stand: Any) -> None:
    model, graph, _ = stand
    model.responses = [
        guard("ДИБ"),
        {
            "content": (
                "Сотрудник сообщает об инциденте [f:dib-project-activity@1#2.7].\n\nНЕТ_ОТВЕТА"
            )
        },
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Как сообщить об инциденте в ДИБ?")]}, config=CONFIG
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "not_found"
    assert "НЕТ_ОТВЕТА" not in reply.text


@pytest.mark.leak
@pytest.mark.parametrize("correction_supported", [True, False])
async def test_comparison_checks_claims_and_retries_at_most_once(
    stand: Any, correction_supported: bool
) -> None:
    model, graph, _ = stand
    model.fragments.extend(
        [
            Fragment(
                fragment_id="subcontractor-selection@1#1.1",
                doc_id="subcontractor-selection",
                version=1,
                title="Регламент подбора субподрядчика",
                clause="1.1",
                area="общий",
                kind="regulation",
                text="При нехватке внутренних ресурсов руководитель проекта формирует запрос.",
                score=0.95,
            ),
            Fragment(
                fragment_id="project-activity-organization@1#2.10",
                doc_id="project-activity-organization",
                version=1,
                title="Организация проектной деятельности",
                clause="2.10",
                area="общий",
                kind="regulation",
                text="При отсутствии внутренних ресурсов TPS формирует критерии выбора.",
                score=0.90,
            ),
            Fragment(
                fragment_id="dib-presale-activity@1#6",
                doc_id="dib-presale-activity",
                version=1,
                title="Пресейловая деятельность Дирекции ИБ",
                clause="6",
                area="ДИБ",
                kind="regulation",
                text="Пресейл-архитектор подбирает партнёра или субподрядчика на пресейле.",
                score=0.85,
            ),
        ]
    )
    direct = "В общем порядке РП формирует запрос [f:subcontractor-selection@1#1.1]."
    good = direct + " В ДИБ пресейл-архитектор выполняет подбор [f:dib-presale-activity@1#6]."
    wrong = direct + " Отдельный запрос от РП не нужен [f:project-activity-organization@1#2.10]."
    correction = good if correction_supported else wrong
    model.responses = [
        guard("общий", "ДИБ"),
        {"content": wrong},
        {"content": json.dumps({"supported": False})},
        {"content": correction},
        {"content": json.dumps({"supported": correction_supported})},
    ]
    events = [
        event
        async for event in graph.astream(
            {"messages": [("human", "Сравни общий порядок и ДИБ при подборе субподрядчика.")]},
            config=CONFIG,
            stream_mode=["values", "messages"],
            subgraphs=True,
        )
    ]
    public_states = [payload for ns, mode, payload in events if not ns and mode == "values"]
    messages = public_states[-1]["messages"]
    reply = messages[-1]
    assert meta(reply).outcome == ("answer" if correction_supported else "fallback")
    assert "отдельный запрос" not in repr(events).lower()
    assert model.fragments[1].text not in repr(events)
    assert len(messages) == 2
    assert not model.responses
    cited = json.loads(model.requests[2]["messages"][-1]["content"])["источники"]
    assert [item["id"] for item in cited] == [
        fragment.fragment_id for fragment in model.fragments[1:3]
    ]
    assert cited[0]["текст"] == model.fragments[1].text
    assert model.requests[2].get("tools") is None
    if correction_supported:
        assert reply.text == good
        assert [source.fragment_id for source in meta(reply).sources] == [
            model.fragments[1].fragment_id,
            model.fragments[3].fragment_id,
        ]


async def test_off_topic_refusal_does_not_search(stand: Any) -> None:
    model, graph, lookups = stand
    model.responses = [guard(on_topic=False)]
    state = await graph.ainvoke(
        {"messages": [("human", "Посчитай маржу моей сделки.")]}, config=CONFIG
    )
    assert meta(state["messages"][-1]).outcome == "refusal"
    assert not lookups
    assert len(model.requests) == 1


async def test_guard_prompt_includes_customer_worded_regulation_questions(stand: Any) -> None:
    model, graph, _ = stand
    model.responses = [guard(on_topic=False)]
    await graph.ainvoke(
        {"messages": [("human", "Оплата прошла, а ключ не пришёл. Что делать?")]}, config=CONFIG
    )
    prompt = model.requests[0]["messages"][0]["content"]
    assert "ключ не пришёл после оплаты" in prompt


async def test_account_limit_applies_before_provider_calls(stand: Any) -> None:
    model, graph, lookups = stand
    model.responses = [guard(on_topic=False)] * 20
    for _ in model.responses.copy():
        state = await graph.ainvoke(
            {"messages": [("human", "Посчитай маржу сделки.")]}, config=CONFIG
        )
        assert meta(state["messages"][-1]).outcome == "refusal"
    limited = await graph.ainvoke({"messages": [("human", "Кто назначает РП?")]}, config=CONFIG)
    assert "лимит" in limited["messages"][-1].text.lower()
    assert len(model.requests) == 20
    assert not lookups


async def test_tool_mode_parses_guard_result() -> None:
    from regagent_backend.guard import classify

    model = Model()
    model.responses = [
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "guard-1",
                    "type": "function",
                    "function": {"name": "GuardResult", "arguments": guard("ДИБ")["content"]},
                }
            ],
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(model.request)) as transport:
        chat = ChatOpenAI(
            model="fake",
            base_url="http://llm/v1",
            api_key=SecretStr("fake"),
            http_async_client=transport,
        )
        result = await classify(chat, ["Дирекция ИБ"], ["ДИБ"], True, "tool")
    assert result.on_topic and result.explicit_areas == ["ДИБ"]
    assert model.requests[0]["tool_choice"]["function"]["name"] == "GuardResult"


@pytest.mark.leak
async def test_raw_event_stream_hides_guard_model_and_search_events(stand: Any) -> None:
    model, graph, _ = stand
    model.responses = [
        guard("ДИБ"),
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    events = [
        event
        async for event in graph.astream_events(
            {"messages": [("human", "Кто назначает РП в ДИБ?")]},
            config=CONFIG,
            version="v2",
        )
    ]
    assert events
    assert all(not event["event"].startswith(("on_chat_model", "on_tool")) for event in events)
    assert "explicit_areas" not in repr(events)
    assert PUNKT.text not in repr(events)


async def test_expired_area_is_confirmed_before_an_unscoped_search(stand: Any) -> None:
    model, graph, lookups = stand
    await graph.store.aput(
        ("users", "reader", "memory"),
        "area:ДИБ",
        {
            "kind": "area",
            "value": "ДИБ",
            "source": "stated",
            "confirmed_at": "2000-01-01T00:00:00Z",
            "times_confirmed": 1,
        },
    )
    model.responses = [guard()]
    answer = await graph.ainvoke({"messages": [("human", "Кто назначает РП?")]}, config=CONFIG)
    assert meta(answer["messages"][-1]).outcome == "ask_area"
    assert not lookups


async def test_initial_search_grounds_answer_without_a_model_tool_call(stand: Any) -> None:
    model, graph, lookups = stand
    model.responses = [
        guard("ДИБ"),
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    result = await graph.ainvoke(
        {"messages": [("human", "Кто назначает РП в ДИБ?")]}, config=CONFIG
    )
    assert meta(result["messages"][-1]).outcome == "answer"
    assert [lookup["areas"] for lookup in lookups] == [["ДИБ"]]


async def test_guard_splits_multiple_questions_before_answering(stand: Any) -> None:
    model, graph, lookups = stand
    first = "Кто запрашивает спецификацию на ПО?"
    second = "Кто назначает РП после уведомления из CRM?"
    classification = json.loads(guard("ДИБ")["content"])
    classification["search_queries"] = [first, second]
    model.responses = [
        {"content": json.dumps(classification)},
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    result = await graph.ainvoke({"messages": [("human", first + " А " + second)]}, config=CONFIG)
    assert meta(result["messages"][-1]).outcome == "answer"
    assert {lookup["query"] for lookup in lookups} == {first, second}
    assert all(lookup["areas"] == ["ДИБ"] for lookup in lookups)


async def test_explicit_second_question_is_searched_even_when_guard_does_not_split(
    stand: Any,
) -> None:
    model, graph, lookups = stand
    model.responses = [
        guard("ДИБ"),
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    question = "Кто запрашивает спецификацию на ПО, а кто назначает РП после уведомления из CRM?"
    await graph.ainvoke({"messages": [("human", question)]}, config=CONFIG)
    assert {lookup["query"] for lookup in lookups} == {
        "Кто запрашивает спецификацию на ПО",
        "кто назначает РП после уведомления из CRM?",
    }


async def test_guard_retries_a_transient_connection_error() -> None:
    from regagent_backend.guard import classify

    model = Model()
    model.responses = [guard("ДИБ")]
    attempts = 0

    async def request(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("transient")
        return await model.request(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as transport:
        chat = ChatOpenAI(
            model="fake",
            base_url="http://llm/v1",
            api_key=SecretStr("fake"),
            http_async_client=transport,
            max_retries=0,
        )
        result = await classify(chat, ["Дирекция ИБ"], ["ДИБ"], True, "json_schema")
    assert attempts == 2
    assert result.explicit_areas == ["ДИБ"]


async def test_clarifies_missing_detail_and_keeps_the_original_question(stand: Any) -> None:
    model, graph, lookups = stand
    model.responses = [
        guard("ДИБ"),
        {"content": "УТОЧНЕНИЕ: Вы спрашиваете о пресейле или об открытом проекте?"},
    ]
    first = await graph.ainvoke(
        {"messages": [("human", "В ДИБ кто отвечает за передачу материалов?")]}, config=CONFIG
    )
    reply = first["messages"][-1]
    assert meta(reply).outcome == "ask_clarification"
    assert reply.text == "Вы спрашиваете о пресейле или об открытом проекте?"
    assert meta(reply).sources == []

    model.responses = [
        guard(),
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    second = await graph.ainvoke(
        {"messages": [*first["messages"], ("human", "Открытый проект.")]}, config=CONFIG
    )
    assert meta(second["messages"][-1]).outcome == "answer"
    assert lookups[-1]["areas"] == ["ДИБ"]
    assert "кто отвечает за передачу материалов" in lookups[-1]["query"]
    assert "Открытый проект" in lookups[-1]["query"]


async def test_missing_answer_offers_qc_and_drafts_only_on_request(stand: Any) -> None:
    model, graph, lookups = stand
    question = "В ДИБ есть компенсация парковки при удалённой работе?"
    model.responses = [guard("ДИБ"), {"content": "НЕТ_ОТВЕТА"}]
    first = await graph.ainvoke({"messages": [("human", question)]}, config=CONFIG)
    reply = first["messages"][-1]
    assert meta(reply).outcome == "not_found"
    assert meta(reply).sources == []
    assert "Передать в ОКК" in reply.text and "могу составить черновик" in reply.text
    assert "Черновик заявки" not in reply.text

    draft_guard = json.loads(guard()["content"])
    draft_guard["qc_action"] = "draft"
    model.responses = [{"content": json.dumps(draft_guard)}]
    second = await graph.ainvoke(
        {"messages": [*first["messages"], ("human", "Составь черновик заявки в ОКК.")]},
        config=CONFIG,
    )
    draft = second["messages"][-1]
    assert meta(draft).outcome == "qc_help"
    assert question in draft.text and "Черновик заявки" in draft.text
    assert len(lookups) == 1

    guide_guard = {**draft_guard, "qc_action": "guide"}
    model.responses = [{"content": json.dumps(guide_guard)}]
    third = await graph.ainvoke(
        {"messages": [*second["messages"], ("human", "Передай заявку сам.")]}, config=CONFIG
    )
    guidance = third["messages"][-1]
    assert meta(guidance).outcome == "qc_help"
    assert "подтвердите отправку" in guidance.text
    assert "заявка отправлена" not in guidance.text.lower()
    assert len(lookups) == 1


async def test_reaching_clarification_limit_offers_qc(stand: Any) -> None:
    model, graph, lookups = stand
    turns: list[Any] = []
    questions = ("В ДИБ кто проверяет расчёт?", "Расчёт по проекту.", "Не знаю.")
    for index, question in enumerate(questions):
        model.responses = [
            guard("ДИБ") if index == 0 else guard(),
            {"content": f"УТОЧНЕНИЕ: Какой тип расчёта вы имеете в виду ({index + 1})?"},
        ]
        state = await graph.ainvoke({"messages": [*turns, ("human", question)]}, config=CONFIG)
        turns = state["messages"]

    last = turns[-1]
    assert meta(last).outcome == "not_found"
    assert "Передать в ОКК" in last.text
    assert "кто проверяет расчёт" in lookups[-1]["query"]
    assert "Расчёт по проекту." in lookups[-1]["query"]


async def test_unavailable_search_does_not_offer_qc_for_a_false_gap(stand: Any) -> None:
    model, graph, _ = stand
    model.search_status = 503
    model.responses = [guard("ДИБ")]
    state = await graph.ainvoke(
        {"messages": [("human", "Кто назначает руководителя проекта в ДИБ?")]}, config=CONFIG
    )
    last = state["messages"][-1]
    assert meta(last).outcome == "fallback"
    assert "сейчас недоступен" in last.text
    assert "Передать в ОКК" not in last.text


async def test_missing_calculation_type_does_not_guess_a_regulation(stand: Any) -> None:
    model, graph, lookups = stand
    classification = json.loads(guard("общий")["content"])
    classification["missing_detail"] = "Какой именно расчёт нужно проверить?"
    model.responses = [
        {"content": json.dumps(classification)},
        {"content": "Срок — один рабочий день [f:dib-project-activity@1#2.7]."},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Сколько времени даётся на проверку расчёта по общему порядку?")]},
        config=CONFIG,
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "ask_clarification"
    assert reply.text == "Какой именно расчёт нужно проверить?"
    assert not lookups


async def test_cited_inconclusive_answer_routes_to_qc(stand: Any) -> None:
    model, graph, _ = stand
    draft = (
        "Подтвердить или опровергнуть наличие порядка компенсации я не могу. "
        "Найденный регламент регулирует другой вопрос [f:dib-project-activity@1#2.7]."
    )
    model.responses = [
        guard("ДИБ"),
        {"content": draft},
        {"content": '{"outcome":"not_found","question":null}'},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Как в ДИБ компенсируют парковку при удалённой работе?")]},
        config=CONFIG,
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "not_found"
    assert not meta(reply).sources
    assert "Передать в ОКК" in reply.text
    assert "другой вопрос" not in reply.text
    evidence = model.requests[-1]["messages"][1]["content"]
    assert "парковку" in evidence and PUNKT.text in evidence


async def test_area_only_reply_retries_original_question_after_fallback(stand: Any) -> None:
    model, graph, lookups = stand
    wrong = {"content": "Ответ со старым пунктом [f:unknown@1#1]."}
    model.responses = [guard("ДИБ"), wrong, wrong]
    first = await graph.ainvoke(
        {"messages": [("human", "Кто назначает руководителя проекта?")]}, config=CONFIG
    )
    assert meta(first["messages"][-1]).outcome == "fallback"

    classification = json.loads(guard("ДИБ")["content"])
    classification["area_only"] = True
    model.responses = [
        {"content": json.dumps(classification)},
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    second = await graph.ainvoke(
        {"messages": [*first["messages"], ("human", "Для Дирекции ИБ.")]}, config=CONFIG
    )
    assert meta(second["messages"][-1]).outcome == "answer"
    assert lookups[-1]["query"] == "Кто назначает руководителя проекта?"
    assert lookups[-1]["areas"] == ["ДИБ"]


async def test_evidence_check_keeps_a_directly_supported_negative_answer(stand: Any) -> None:
    model, graph, _ = stand
    clause = "Координатору запрещено подтверждать назначение РП при неполном расчёте."
    model.fragments = [PUNKT.model_copy(update={"text": clause})]
    supported_draft = "Подтвердить назначение нельзя [f:dib-project-activity@1#2.7]."
    model.responses = [
        guard("ДИБ"),
        {"content": supported_draft},
        {"content": '{"outcome":"answer","question":null}'},
    ]
    state = await graph.ainvoke(
        {"messages": [("human", "Можно подтвердить назначение РП в ДИБ без полного расчёта?")]},
        config=CONFIG,
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "answer"
    assert meta(reply).sources[0].fragment_id == PUNKT.fragment_id
    assert reply.text == supported_draft


async def test_area_question_from_guard_uses_area_outcome(stand: Any) -> None:
    model, graph, _ = stand
    classification = json.loads(guard()["content"])
    classification["missing_detail"] = "В какой области действует правило назначения РП?"
    model.responses = [{"content": json.dumps(classification)}]
    state = await graph.ainvoke(
        {"messages": [("human", "Кто назначает руководителя проекта?")]}, config=CONFIG
    )
    assert meta(state["messages"][-1]).outcome == "ask_area"


async def test_answers_clarification_instead_of_repeating_guard_question(stand: Any) -> None:
    model, graph, lookups = stand
    classification = json.loads(guard("ДИБ")["content"])
    classification["missing_detail"] = "Какой тип расчёта нужно проверить?"
    model.responses = [{"content": json.dumps(classification)}]
    first = await graph.ainvoke(
        {"messages": [("human", "В ДИБ кто проверяет расчёт?")]}, config=CONFIG
    )
    assert meta(first["messages"][-1]).outcome == "ask_clarification"
    model.responses = [
        {"content": json.dumps(classification)},
        {"content": "Координатор [f:dib-project-activity@1#2.7]."},
    ]
    second = await graph.ainvoke(
        {"messages": [*first["messages"], ("human", "Расчёт проекта, комплект полный.")]},
        config=CONFIG,
    )
    assert meta(second["messages"][-1]).outcome == "answer"
    assert "Расчёт проекта, комплект полный" in lookups[-1]["query"]


async def test_divergent_search_does_not_force_area_for_an_unanswered_question(stand: Any) -> None:
    model, graph, _ = stand
    model.responses = [guard(), {"content": "НЕТ_ОТВЕТА"}]
    state = await graph.ainvoke(
        {"messages": [("human", "Как компенсируют платную парковку при удалённой работе?")]},
        config=CONFIG,
    )
    reply = state["messages"][-1]
    assert meta(reply).outcome == "not_found"
    assert "Передать в ОКК" in reply.text
