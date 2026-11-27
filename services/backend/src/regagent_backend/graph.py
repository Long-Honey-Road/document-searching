"""Граф Aegra. В диалог записываются только сообщения пользователя и проверенные ответы."""

from __future__ import annotations

import logging
import re
from asyncio import create_task, gather
from contextvars import Context
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal, TypedDict

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.config import get_config
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from openai import OpenAIError
from pydantic import SecretStr
from regagent_common.contracts import AnswerMeta, AnswerSource
from regagent_common.settings import CommonSettings

from regagent_backend.agent import generate_answer, last_answer
from regagent_backend.citations import extract_citations, public_text, verify_citations
from regagent_backend.guard import PROMPTS, GuardResult, classify
from regagent_backend.identity import UserIdentity, current_identity, identity_to_user_dict
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.memory import MemoryService, build_memory_tools
from regagent_backend.rate_limit import AccountRateLimit
from regagent_backend.settings import BackendSettings
from regagent_backend.tools import SearchRun, build_search_tools

log = logging.getLogger(__name__)
AreaSource = Literal["memory", "question", "asked", "none"]
Outcome = Literal["answer", "refusal", "not_found", "ask_area", "fallback"]

FALLBACK = "Не удалось надёжно ответить по регламентам. Попробуйте уточнить вопрос."
REFUSAL = (
    "Я отвечаю по внутренним регламентам компании. "
    "Могу рассказать, какой порядок установлен регламентом."
)
NOT_FOUND = "В доступных материалах не нашёл сведений по этому вопросу."
ASK_AREA = "Уточните, для какой области нужен порядок по регламенту."
_SECOND_QUESTION = re.compile(
    r"(?<=[,;?])\s+(?:а|и)\s+(?=(?:кто|что|как|когда|где|куда|сколько|какой|какие)\b)",
    re.IGNORECASE,
)


class Conversation(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


class State(Conversation, total=False):
    result: AIMessage | None


@dataclass(frozen=True)
class AgentDeps:
    settings: BackendSettings
    guard_model: BaseChatModel
    agent_model: BaseChatModel
    knowledge: KnowledgeClient


@lru_cache(maxsize=1)
def _runtime_deps() -> AgentDeps:
    settings = BackendSettings()
    common = CommonSettings()
    token = settings.KNOWLEDGE_INTERNAL_TOKEN.get_secret_value()
    if not token:
        raise ValueError("KNOWLEDGE_INTERNAL_TOKEN is required")

    def chat(role: Literal["agent", "guard"]) -> ChatOpenAI:
        return ChatOpenAI(
            model=common.model_for(role),
            base_url=common.llm_base_url,
            api_key=common.llm_api_key or SecretStr("unused"),
            timeout=common.llm_timeout_s,
            extra_body=common.llm_extra_body_json,
            max_retries=0,
            use_responses_api=False,
            disable_streaming=True,
        )

    http = httpx.AsyncClient(timeout=settings.KNOWLEDGE_TIMEOUT_S)
    return AgentDeps(
        settings, chat("guard"), chat("agent"), KnowledgeClient(settings.KNOWLEDGE_URL, token, http)
    )


def _reply(
    text: str,
    outcome: Outcome,
    *,
    area: str | None = None,
    area_source: AreaSource = "none",
    sources: list[AnswerSource] | None = None,
) -> AIMessage:
    meta = AnswerMeta(
        verified=True,
        area_used=area,
        area_source=area_source,
        sources=sources or [],
        outcome=outcome,
    )
    return AIMessage(content=text, additional_kwargs={"answer_meta": meta.model_dump()})


def _awaiting_area(messages: list[BaseMessage]) -> bool:
    previous = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)), None
    )
    if previous is None:
        return False
    answer_meta = previous.additional_kwargs.get("answer_meta")
    return isinstance(answer_meta, dict) and answer_meta.get("outcome") == "ask_area"


async def _memory_action(
    memory: MemoryService,
    identity: UserIdentity,
    guard: GuardResult,
    areas: tuple[str, ...],
    config: RunnableConfig,
) -> AIMessage:
    if len(areas) != 1:
        return _reply("Уточните, какую область нужно запомнить или забыть.", "ask_area")
    area = areas[0]
    tools = build_memory_tools(memory, remember_area=area)
    tool = next(tool for tool in tools if tool.name == guard.memory_action)
    await tool.ainvoke({"kind": "area", "value": area}, config=config)
    action = "Запомнил" if guard.memory_action == "remember" else "Удалил из памяти"
    log.info("user=%s memory_action=%s", identity.sub, guard.memory_action)
    return _reply(f"{action} область «{area}».", "answer")


def _reply_with_sources(
    text: str, run: SearchRun, areas: tuple[str, ...], source: AreaSource
) -> AIMessage:
    sources = [
        AnswerSource(
            n=index,
            fragment_id=fragment.fragment_id,
            title=fragment.title,
            clause=fragment.clause,
            version=fragment.version,
            kind=fragment.kind,
        )
        for index, fragment in enumerate(
            (run.fragments[citation] for citation in extract_citations(text)), 1
        )
    ]
    area = areas[0] if len(areas) == 1 else None
    if area is None and not areas:
        used = {run.fragments[ref.fragment_id].area for ref in sources}
        if len(used) == 1:
            area = used.pop()
    return _reply(text, "answer", area=area, area_source=source, sources=sources)


async def _answer(
    deps: AgentDeps,
    identity: UserIdentity,
    questions: list[str],
    awaiting_area: bool,
    search_queries: list[str],
    memory_line: str,
    areas: tuple[str, ...],
    area_source: AreaSource,
) -> tuple[AIMessage, SearchRun]:
    run = SearchRun()
    settings = deps.settings
    tools = build_search_tools(
        deps.knowledge,
        run,
        area_codes=set(settings.AREA_CODES),
        default_areas=areas,
        max_searches=settings.AGENT_MAX_SEARCHES,
        result_max_chars=settings.TOOL_RESULT_MAX_CHARS,
    )
    prompt = (PROMPTS / "system.md").read_text(encoding="utf-8")
    prompt += "\n\nДопустимые области: " + ", ".join(settings.AREA_CODES)
    if memory_line:
        prompt += "\n" + memory_line
    if areas:
        prompt += "\nОбласти текущего запроса: " + ", ".join(areas)
    # Предыдущие вопросы помогают разрешать короткие продолжения. Ответы и сырые результаты
    # прошлого запуска не передаются модели и не могут подтвердить ссылку.
    history: list[AnyMessage] = [HumanMessage(question) for question in questions]
    initial_query = "\n".join(questions[-2:]) if awaiting_area else questions[-1]
    queries = list(
        dict.fromkeys(query.strip()[: settings.QUESTION_MAX_CHARS] for query in search_queries)
    )
    queries = [query for query in queries if query][:2]
    if len(queries) < 2 and not awaiting_area:
        queries = [part.strip(" ,;") for part in _SECOND_QUESTION.split(initial_query, maxsplit=1)]
    if len(queries) < 2 or settings.AGENT_MAX_SEARCHES == 1:
        queries = [initial_query]
    calls = [
        ToolCall(name="search_docs", args={"query": query}, id=f"initial-search-{i}")
        for i, query in enumerate(queries, 1)
    ]
    tool_config: RunnableConfig = {
        "configurable": {"langgraph_auth_user": identity_to_user_dict(identity)}
    }
    initial_results = await gather(
        *(
            create_task(tools[0].ainvoke(call["args"], config=tool_config), context=Context())
            for call in calls
        ),
    )
    if not areas and any(result.areas_diverge for _, result in run.searches):
        return _reply(ASK_AREA, "ask_area"), run
    history.append(AIMessage(content="", tool_calls=calls))
    history.extend(
        ToolMessage(content=initial, tool_call_id=call["id"] or "")
        for call, initial in zip(calls, initial_results, strict=True)
    )
    for attempt in range(2):
        try:
            history = await generate_answer(
                deps.agent_model, prompt, history, tools, identity, settings.AGENT_MAX_STEPS
            )
        except (OpenAIError, GraphRecursionError) as exc:
            log.warning("answer generation failed: %s", type(exc).__name__)
            if run.searches and all(not result.fragments for _, result in run.searches):
                return _reply(NOT_FOUND, "not_found"), run
            break
        answer = last_answer(history)
        text = public_text(answer) if answer is not None else ""
        citations = verify_citations(text, set(run.fragments))
        if not areas and any(result.areas_diverge for _, result in run.searches):
            return _reply(ASK_AREA, "ask_area"), run
        if citations.ok:
            return _reply_with_sources(text, run, areas, area_source), run
        if (
            "[f:" not in text
            and run.searches
            and all(result.low_confidence or not result.fragments for _, result in run.searches)
        ):
            return _reply(NOT_FOUND, "not_found"), run
        if attempt == 0:
            history.append(
                HumanMessage(
                    "Предыдущий ответ не прошёл проверку ссылок. "
                    "Используй только ссылки из результатов этого запуска. "
                    "Если данных недостаточно, сообщи об этом."
                )
            )
    return _reply(FALLBACK, "fallback"), run


async def _turn(
    deps: AgentDeps,
    state: State,
    identity: UserIdentity,
    config: RunnableConfig,
    store: BaseStore | None,
) -> AIMessage:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], HumanMessage):
        return _reply(REFUSAL, "refusal")
    questions = [message.text.strip() for message in messages if isinstance(message, HumanMessage)]
    limit = deps.settings.QUESTION_MAX_CHARS
    if not questions[-1] or len(questions[-1]) > limit:
        return _reply(f"Задайте вопрос длиной до {limit} символов.", "refusal")
    questions = [question[-limit:] for question in questions[-6:]]
    awaiting = _awaiting_area(messages)
    settings = deps.settings
    try:
        guard = await classify(
            deps.guard_model, questions, settings.AREA_CODES, awaiting, settings.GUARD_MODE
        )
    except (OpenAIError, OutputParserException, ValueError) as exc:
        log.warning("guard failed: %s", type(exc).__name__)
        return _reply(FALLBACK, "fallback")
    if guard.injection or not guard.on_topic or guard.partial_topic:
        return _reply(REFUSAL, "refusal")

    allowed = set(settings.AREA_CODES)
    areas = tuple(dict.fromkeys(area for area in guard.explicit_areas if area in allowed))
    source: AreaSource = ("asked" if awaiting else "question") if areas else "none"
    memory = (
        MemoryService(
            store, allowed, settings.MEMORY_MAX_ACTIVE_AREAS, settings.MEMORY_AREA_TTL_DAYS
        )
        if store is not None
        else None
    )
    if guard.memory_action != "none":
        if memory is None:
            return _reply(FALLBACK, "fallback")
        return await _memory_action(memory, identity, guard, areas, config)

    now = datetime.now(UTC)
    records = await memory.load(identity.sub) if memory is not None else []
    memory_line = memory.render_for_prompt(records, now) if memory is not None else ""
    if not areas and not awaiting and memory is not None:
        recent = next((record for record in records if not memory.is_stale(record, now)), None)
        if recent is not None:
            areas, source = (recent.value,), "memory"
        elif records:
            return _reply(ASK_AREA, "ask_area")

    reply, run = await _answer(
        deps, identity, questions, awaiting, guard.search_queries, memory_line, areas, source
    )
    meta = AnswerMeta.model_validate(reply.additional_kwargs["answer_meta"])
    if (
        memory is not None
        and meta.outcome == "answer"
        and source in {"question", "asked"}
        and len(areas) == 1
        and any(filters == list(areas) for filters, _ in run.searches)
    ):
        await memory.remember(
            identity.sub, "area", areas[0], "confirmed" if source == "asked" else "stated"
        )
    log.info(
        "user=%s outcome=%s fragments=%s",
        identity.sub,
        meta.outcome,
        sorted(run.fragments),
    )
    return reply


def build_graph(
    deps: AgentDeps | None = None, *, store: BaseStore | None = None
) -> CompiledStateGraph[State, None, Conversation, Conversation]:
    rate_limit = AccountRateLimit()

    async def agent(state: State, runtime: Runtime) -> dict[str, AIMessage]:
        config = get_config()
        identity = current_identity(config)
        active_deps = deps or _runtime_deps()
        if not rate_limit.allow(identity.sub, active_deps.settings.RATE_LIMIT_PER_USER_PER_MIN):
            return {
                "result": _reply("Лимит запросов на минуту исчерпан. Попробуйте позже.", "refusal")
            }
        return {"result": await _turn(active_deps, state, identity, config, runtime.store)}

    async def finalize(state: State) -> dict[str, object]:
        result = state.get("result")
        return {"messages": [result or _reply(FALLBACK, "fallback")], "result": None}

    builder = StateGraph(State, input_schema=Conversation, output_schema=Conversation)
    builder.add_node("agent", agent)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(store=store)


graph = build_graph()
