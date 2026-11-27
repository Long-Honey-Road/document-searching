"""Граф Aegra. В диалог записываются только сообщения пользователя и проверенные ответы."""

from __future__ import annotations

import logging
import re
import time
from asyncio import create_task, gather, wait_for
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal, Protocol, TypedDict
from uuid import uuid4

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
from regagent_common.contracts import (
    AnswerMeta,
    AnswerOutcome,
    AnswerSource,
    TurnOutcome,
    TurnReport,
)
from regagent_common.settings import CommonSettings

from regagent_backend.agent import generate_answer, last_answer
from regagent_backend.citations import (
    extract_citations,
    missing_overrides,
    public_text,
    verify_citations,
    verify_comparison,
)
from regagent_backend.dialogue import (
    CLARIFY_PREFIX,
    MAX_DETAIL_QUESTIONS,
    NO_ANSWER,
    NOT_FOUND,
    QC_GUIDE,
    Topic,
    current_topic,
    parse_clarification,
    qc_draft,
    stored_meta,
)
from regagent_backend.events import events, run_fields
from regagent_backend.guard import PROMPTS, GuardResult, classify
from regagent_backend.identity import UserIdentity, current_identity, identity_to_user_dict
from regagent_backend.knowledge_client import KnowledgeClient
from regagent_backend.memory import MemoryService, build_memory_tools, memory_service
from regagent_backend.qc_turn_sink import QcTurnSink
from regagent_backend.rate_limit import AccountRateLimit
from regagent_backend.settings import BackendSettings
from regagent_backend.tools import SearchRun, build_search_tools
from regagent_backend.usage import UsageCallback

log = logging.getLogger(__name__)
AreaSource = Literal["memory", "question", "asked", "none"]

FALLBACK = "Не удалось надёжно ответить по регламентам. Попробуйте уточнить вопрос."
REFUSAL = (
    "Я отвечаю по внутренним регламентам компании. "
    "Могу рассказать, какой порядок установлен регламентом."
)
SEARCH_UNAVAILABLE = "Поиск по регламентам сейчас недоступен. Попробуйте задать вопрос позже."
ASK_AREA = "Уточните, для какой области нужен порядок по регламенту."
_SECOND_QUESTION = re.compile(
    r"(?<=[,;?])\s+(?:а|и)\s+(?=(?:кто|что|как|когда|где|куда|сколько|какой|какие)\b)",
    re.IGNORECASE,
)


@dataclass
class TurnTrace:
    """Что известно о ходе для события ``turn.result``: причина исхода и данные поиска."""

    reason: str | None = None
    error_code: str | None = None
    run: SearchRun = field(default_factory=SearchRun)


_TRACE: ContextVar[TurnTrace | None] = ContextVar("regagent_turn_trace", default=None)


class Conversation(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


class State(Conversation, total=False):
    result: AIMessage | None


class TurnSink(Protocol):
    """Хранилище итогов хода (поток B). Повтор одного ``run_id`` должен давать одну запись."""

    async def record(self, report: TurnReport, identity: UserIdentity) -> None: ...


TURN_REPORT_TIMEOUT_S = 2.0
# Команды памяти и пустые запуски — не вопросы для аналитики ОКК
_NOT_REPORTED = frozenset(
    {
        "no_question",
        "memory_remember",
        "memory_forget",
        "memory_area_unclear",
        "memory_unavailable",
        "qc_draft",
        "qc_guide",
    }
)
_UNSUPPORTED = frozenset(
    {
        "citations_rejected",
        "comparison_rejected",
        "low_confidence",
        "practice_not_cited",
        "no_regulation_support",
    }
)


@dataclass(frozen=True)
class AgentDeps:
    settings: BackendSettings
    guard_model: BaseChatModel
    agent_model: BaseChatModel
    knowledge: KnowledgeClient
    turn_sink: TurnSink | None = None


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
            callbacks=[UsageCallback(role)],
        )

    http = httpx.AsyncClient(timeout=settings.KNOWLEDGE_TIMEOUT_S)
    return AgentDeps(
        settings,
        chat("guard"),
        chat("agent"),
        KnowledgeClient(settings.KNOWLEDGE_URL, token, http),
        turn_sink=QcTurnSink(),
    )


def _reply(
    text: str,
    outcome: AnswerOutcome,
    *,
    area: str | None = None,
    area_source: AreaSource = "none",
    sources: list[AnswerSource] | None = None,
    reason: str | None = None,
) -> AIMessage:
    trace = _TRACE.get()
    if trace is not None and reason is not None:
        trace.reason = reason
    meta = AnswerMeta(
        verified=True,
        area_used=area,
        area_source=area_source,
        sources=sources or [],
        outcome=outcome,
    )
    return AIMessage(content=text, additional_kwargs={"answer_meta": meta.model_dump()})


def _unanswered(run: SearchRun) -> AIMessage:
    if run.unavailable and not run.fragments:
        return _reply(SEARCH_UNAVAILABLE, "fallback", reason="search_unavailable")
    reason = "no_regulation_support" if run.fragments else "empty_search"
    return _reply(NOT_FOUND, "not_found", reason=reason)


async def _memory_action(
    memory: MemoryService,
    identity: UserIdentity,
    guard: GuardResult,
    areas: tuple[str, ...],
    config: RunnableConfig,
) -> AIMessage:
    if len(areas) != 1:
        return _reply(
            "Уточните, какую область нужно запомнить или забыть.",
            "ask_area",
            reason="memory_area_unclear",
        )
    area = areas[0]
    tools = build_memory_tools(memory, remember_area=area)
    tool = next(tool for tool in tools if tool.name == guard.memory_action)
    await tool.ainvoke({"kind": "area", "value": area}, config=config)
    action = "Запомнил" if guard.memory_action == "remember" else "Удалил из памяти"
    log.info("user=%s memory_action=%s", identity.sub, guard.memory_action)
    return _reply(f"{action} область «{area}».", "answer", reason=f"memory_{guard.memory_action}")


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
    continuation: Topic | None,
    search_queries: list[str],
    memory_line: str,
    areas: tuple[str, ...],
    area_source: AreaSource,
    ids: dict[str, str],
) -> tuple[AIMessage, SearchRun]:
    run = SearchRun()
    trace = _TRACE.get()
    if trace is not None:
        trace.run = run
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
    initial_query = "\n".join(continuation.questions) if continuation else questions[-1]
    queries = list(
        dict.fromkeys(query.strip()[: settings.QUESTION_MAX_CHARS] for query in search_queries)
    )
    queries = [query for query in queries if query][:2]
    if len(queries) < 2 and continuation is None:
        queries = [part.strip(" ,;") for part in _SECOND_QUESTION.split(initial_query, maxsplit=1)]
    if continuation is not None or len(queries) < 2 or settings.AGENT_MAX_SEARCHES == 1:
        queries = [initial_query]
    calls = [
        ToolCall(name="search_docs", args={"query": query}, id=f"initial-search-{i}")
        for i, query in enumerate(queries, 1)
    ]
    tool_config: RunnableConfig = {
        "configurable": {"langgraph_auth_user": identity_to_user_dict(identity), **ids}
    }
    initial_results = await gather(
        *(
            create_task(tools[0].ainvoke(call["args"], config=tool_config), context=Context())
            for call in calls
        ),
    )
    if not areas and any(result.areas_diverge for _, result in run.searches):
        return _reply(ASK_AREA, "ask_area", reason="areas_diverge"), run
    if run.unavailable and not run.searches:
        return _unanswered(run), run
    history.append(AIMessage(content="", tool_calls=calls))
    history.extend(
        ToolMessage(content=initial, tool_call_id=call["id"] or "")
        for call, initial in zip(calls, initial_results, strict=True)
    )
    for attempt in range(2):
        try:
            with events.timed("answer.generate", attempt=attempt + 1, **ids):
                history = await generate_answer(
                    deps.agent_model,
                    prompt,
                    history,
                    tools,
                    identity,
                    settings.AGENT_MAX_STEPS,
                    ids,
                )
        except (OpenAIError, GraphRecursionError) as exc:
            log.warning("answer generation failed: %s", type(exc).__name__)
            if trace is not None:
                trace.error_code = type(exc).__name__
            reason = "generation_error"
            break
        answer = last_answer(history)
        text = public_text(answer) if answer is not None else ""
        citations = verify_citations(text, set(run.fragments))
        if not areas and any(result.areas_diverge for _, result in run.searches):
            return _reply(ASK_AREA, "ask_area", reason="areas_diverge"), run
        if text == NO_ANSWER:
            return _unanswered(run), run
        if clarification := parse_clarification(text):
            if continuation and continuation.clarification_count >= MAX_DETAIL_QUESTIONS:
                return _unanswered(run), run
            return _reply(
                clarification,
                "ask_clarification",
                area=areas[0] if len(areas) == 1 else None,
                area_source=area_source,
                reason="missing_detail",
            ), run
        supported = citations.ok and not text.startswith((CLARIFY_PREFIX, NO_ANSWER))
        reason = "citations_rejected"
        overridden = missing_overrides(text, run.fragments) if supported else []
        if overridden:
            supported, reason = False, "practice_not_cited"
        if supported and len(areas) > 1:
            try:
                with events.timed("answer.comparison_check", **ids) as event:
                    supported = await verify_comparison(
                        text,
                        [run.fragments[citation] for citation in extract_citations(text)],
                        deps.guard_model,
                        settings.GUARD_MODE,
                        ids,
                    )
                    event["status"] = "ok" if supported else "rejected"
            except (OpenAIError, OutputParserException, ValueError) as exc:
                log.warning("comparison support check failed: %s", type(exc).__name__)
                if trace is not None:
                    trace.error_code = type(exc).__name__
                reason = "comparison_check_error"
                break
            reason = "comparison_rejected"
        if supported:
            return _reply_with_sources(text, run, areas, area_source), run
        if (
            "[f:" not in text
            and run.searches
            and all(result.low_confidence or not result.fragments for _, result in run.searches)
        ):
            return _unanswered(run), run
        if attempt == 0 and overridden:
            history.append(
                HumanMessage(
                    "Процитированный пункт заменяет практика ОКК "
                    + ", ".join(f"[f:{practice_id}]" for practice_id in overridden)
                    + ". Назови расхождение: что сказано в регламенте и что действует по "
                    "практике, и сошлись на обе записи."
                )
            )
        elif attempt == 0:
            history.append(
                HumanMessage(
                    "Предыдущий ответ не прошёл проверку утверждений и ссылок. "
                    "Сверь каждое утверждение с процитированным пунктом и его областью. "
                    "Удали неподтверждённые и противоречащие друг другу утверждения. "
                    "Используй только ссылки из результатов этого запуска. "
                    "Если нужен один уточняющий ответ, используй формат УТОЧНЕНИЕ. "
                    "Если ни один пункт не подтверждает ответ, верни НЕТ_ОТВЕТА."
                )
            )
    return _reply(FALLBACK, "fallback", reason=reason), run


async def _turn(
    deps: AgentDeps,
    state: State,
    identity: UserIdentity,
    config: RunnableConfig,
    store: BaseStore | None,
) -> AIMessage:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], HumanMessage):
        return _reply(REFUSAL, "refusal", reason="no_question")
    questions = [message.text.strip() for message in messages if isinstance(message, HumanMessage)]
    limit = deps.settings.QUESTION_MAX_CHARS
    if not questions[-1] or len(questions[-1]) > limit:
        return _reply(f"Задайте вопрос длиной до {limit} символов.", "refusal", reason="too_long")
    questions = [question[-limit:] for question in questions[-6:]]
    previous = next(
        (stored_meta(message) for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    awaiting_area = previous is not None and previous.outcome == "ask_area"
    awaiting_detail = previous is not None and previous.outcome == "ask_clarification"
    continuation = current_topic(messages, limit) if awaiting_area or awaiting_detail else None
    settings = deps.settings
    ids = run_fields(config)
    try:
        with events.timed("guard.classify", **ids):
            guard = await classify(
                deps.guard_model,
                questions,
                settings.AREA_CODES,
                awaiting_area,
                settings.GUARD_MODE,
                ids,
                awaiting_detail=awaiting_detail,
            )
    except (OpenAIError, OutputParserException, ValueError) as exc:
        log.warning("guard failed: %s", type(exc).__name__)
        trace = _TRACE.get()
        if trace is not None:
            trace.error_code = type(exc).__name__
        return _reply(FALLBACK, "fallback", reason="guard_error")
    if guard.injection or not guard.on_topic or guard.partial_topic:
        refusal = (
            "injection"
            if guard.injection
            else "partial_topic"
            if guard.partial_topic
            else "off_topic"
        )
        return _reply(REFUSAL, "refusal", reason=refusal)

    if guard.qc_action == "guide":
        return _reply(QC_GUIDE, "qc_help", reason="qc_guide")
    if guard.qc_action == "draft":
        draft = qc_draft(messages, limit)
        return _reply(
            draft or "Напишите отдельным сообщением вопрос, по которому нужен черновик в ОКК.",
            "qc_help",
            reason="qc_draft",
        )

    allowed = set(settings.AREA_CODES)
    areas = tuple(dict.fromkeys(area for area in guard.explicit_areas if area in allowed))
    source: AreaSource = ("asked" if awaiting_area else "question") if areas else "none"
    memory = memory_service(store, settings) if store is not None else None
    if guard.memory_action != "none":
        if memory is None:
            return _reply(FALLBACK, "fallback", reason="memory_unavailable")
        return await _memory_action(memory, identity, guard, areas, config)

    if (
        awaiting_detail
        and not areas
        and previous
        and previous.area_used
        and previous.area_used in allowed
    ):
        areas, source = (previous.area_used,), previous.area_source

    now = datetime.now(UTC)
    records = await memory.load(identity.sub) if memory is not None else []
    memory_line = memory.render_for_prompt(records, now) if memory is not None else ""
    if not areas and not awaiting_area and memory is not None:
        recent = next((record for record in records if not memory.is_stale(record, now)), None)
        if recent is not None:
            areas, source = (recent.value,), "memory"
        elif records:
            # Для демо устаревшая память равна удалённой: переспросить один раз и забыть
            expired = await memory.purge_stale(identity.sub, now)
            events.emit_fields("memory.expired", {"areas": expired, **ids}, status="ok")
            return _reply(ASK_AREA, "ask_area", reason="memory_stale")

    reply, run = await _answer(
        deps,
        identity,
        questions,
        continuation,
        guard.search_queries,
        memory_line,
        areas,
        source,
        ids,
    )
    meta = AnswerMeta.model_validate(reply.additional_kwargs["answer_meta"])
    if (
        memory is not None
        and meta.outcome == "answer"
        and source in {"question", "asked"}
        and len(areas) == 1
        and any(filters == list(areas) for filters, _ in run.searches)
    ):
        # Прямое указание области — сигнал; запись и смена — по правилам D-55
        decision = await memory.observe(
            identity.sub, ids.get("run_id") or uuid4().hex, areas[0], datetime.now(UTC)
        )
        events.emit_fields(
            "memory.area",
            {
                "area": decision.area,
                "count": decision.count,
                "needed": decision.needed,
                "area_source": source,
                **ids,
            },
            status=decision.status,
        )
    log.info(
        "user=%s outcome=%s fragments=%s",
        identity.sub,
        meta.outcome,
        sorted(run.fragments),
    )
    return reply


def report_outcome(
    outcome: str, reason: str | None, error_code: str | None, unavailable: int, fragments: int
) -> TurnOutcome:
    """Исход хода для аналитики ОКК по данным запуска, без сигнала от модели."""
    if outcome == "answer":
        return "answered"
    if unavailable and not fragments:
        return "search_unavailable"
    if outcome == "refusal":
        return "refused"
    if outcome == "ask_area":
        return "ask_area"
    if outcome == "ask_clarification":
        return "ask_clarification"
    if reason in _UNSUPPORTED:
        return "unsupported"
    if outcome == "not_found":
        return "empty_search"
    if error_code is not None and "Timeout" in error_code:
        return "model_timeout"
    return "error"


async def _report_turn(
    sink: TurnSink,
    trace: TurnTrace,
    result: AIMessage,
    state: State,
    identity: UserIdentity,
    config: RunnableConfig,
) -> None:
    ids = run_fields(config)
    if trace.reason in _NOT_REPORTED or "run_id" not in ids:
        return
    question = next(
        (
            m.text.strip()
            for m in reversed(state.get("messages", []))
            if isinstance(m, HumanMessage)
        ),
        "",
    )
    meta = AnswerMeta.model_validate(result.additional_kwargs["answer_meta"])
    run = trace.run
    report = TurnReport(
        run_id=ids["run_id"],
        thread_id=ids.get("thread_id"),
        user_sub=identity.sub,
        at=datetime.now(UTC),
        question=question,
        area=meta.area_used,
        area_source=meta.area_source,
        outcome=report_outcome(
            meta.outcome, trace.reason, trace.error_code, run.unavailable, len(run.fragments)
        ),
        reason=trace.reason,
        fragments=list(run.fragments),
        cited=[source.fragment_id for source in meta.sources],
        practice_used=any(source.kind == "practice" for source in meta.sources),
        searches=len(run.searches),
    )
    started = time.perf_counter()
    try:
        await wait_for(sink.record(report, identity), TURN_REPORT_TIMEOUT_S)
    except Exception as exc:  # аналитика не должна менять уже готовый ответ
        events.emit(
            "turn.report",
            status="error",
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=type(exc).__name__,
            **ids,
        )
        return
    events.emit(
        "turn.report",
        status="ok",
        duration_ms=(time.perf_counter() - started) * 1000,
        outcome=report.outcome,
        **ids,
    )


def _emit_turn(
    trace: TurnTrace,
    result: AIMessage,
    identity: UserIdentity,
    config: RunnableConfig,
    seconds: float,
) -> None:
    meta = AnswerMeta.model_validate(result.additional_kwargs["answer_meta"])
    run = trace.run
    events.emit(
        "turn.result",
        status="ok",
        duration_ms=seconds * 1000,
        error_code=trace.error_code,
        outcome=meta.outcome,
        reason=trace.reason,
        area=meta.area_used,
        area_source=meta.area_source,
        searches=len(run.searches),
        search_unavailable=run.unavailable,
        fragments=len(run.fragments),
        sources=len(meta.sources),
        user=identity.sub,
        **run_fields(config),
    )


def build_graph(
    deps: AgentDeps | None = None, *, store: BaseStore | None = None
) -> CompiledStateGraph[State, None, Conversation, Conversation]:
    rate_limit = AccountRateLimit()

    async def agent(state: State, runtime: Runtime) -> dict[str, AIMessage]:
        config = get_config()
        identity = current_identity(config)
        trace = TurnTrace()
        token = _TRACE.set(trace)
        started = time.perf_counter()
        try:
            active_deps = deps or _runtime_deps()
            if not rate_limit.allow(identity.sub, active_deps.settings.RATE_LIMIT_PER_USER_PER_MIN):
                result = _reply(
                    "Лимит запросов на минуту исчерпан. Попробуйте позже.",
                    "refusal",
                    reason="rate_limit",
                )
            else:
                result = await _turn(active_deps, state, identity, config, runtime.store)
        except BaseException as exc:
            events.emit(
                "turn.result",
                status="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                error_code=type(exc).__name__,
                user=identity.sub,
                **run_fields(config),
            )
            raise
        finally:
            _TRACE.reset(token)
        _emit_turn(trace, result, identity, config, time.perf_counter() - started)
        sink = (deps or _runtime_deps()).turn_sink
        if sink is not None:
            await _report_turn(sink, trace, result, state, identity, config)
        return {"result": result}

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
