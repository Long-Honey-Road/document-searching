"""Инструменты поиска. Контекст полученных фрагментов живёт только в текущем запуске."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from regagent_common.contracts import (
    ClauseRequest,
    DocumentRef,
    Fragment,
    RefsRequest,
    SearchRequest,
    SearchResponse,
)

from regagent_backend.events import events, run_fields
from regagent_backend.identity import current_identity
from regagent_backend.knowledge_client import KnowledgeClient, KnowledgeUnavailable

_REGULATION_ID = re.compile(r"(?P<doc>[^\s#]+)@(?P<version>[0-9]{1,18})#(?P<clause>[^\s#]+)")
_PRACTICE_ID = re.compile(r"practice:[^\s#]+")


@dataclass
class SearchRun:
    fragments: dict[str, Fragment] = field(default_factory=dict)
    searches: list[tuple[list[str], SearchResponse]] = field(default_factory=list)
    attempts: int = 0
    unavailable: int = 0


def parse_fragment_id(fragment_id: str) -> tuple[str, int, str] | None:
    match = _REGULATION_ID.fullmatch(fragment_id)
    if not match:
        return None
    return match["doc"], int(match["version"]), match["clause"]


def _show_fragments(
    fragments: list[Fragment], run: SearchRun, max_chars: int, note: str = ""
) -> str:
    blocks: list[str] = []
    remaining = max_chars - len(note)
    for fragment in fragments:
        block = (
            f"[f:{fragment.fragment_id}] {fragment.title}, п. {fragment.clause}, "
            f"область {fragment.area}\n{fragment.text}"
        )
        if len(block) + 2 > remaining:
            break
        blocks.append(block)
        run.fragments[fragment.fragment_id] = fragment
        remaining -= len(block) + 2
    return "\n\n".join([*blocks, *([note] if note else [])])


_NO_REFS = "Связанных документов нет."


def _show_refs(outgoing: list[DocumentRef], incoming: list[DocumentRef], max_chars: int) -> str:
    sections = (
        ("Этот документ ссылается на:", outgoing, "в п. {clause} — «{title}» ({doc_id})"),
        ("На этот документ ссылаются:", incoming, "«{title}» ({doc_id}), п. {clause}"),
    )
    lines: list[str] = []
    remaining = max_chars
    for header, refs, template in sections:
        if not refs:
            continue
        entries = [template.format(**ref.model_dump()) for ref in refs]
        for line in [header, *(f"- {entry}" for entry in entries)]:
            if len(line) + 1 > remaining:
                return "\n".join(lines)
            lines.append(line)
            remaining -= len(line) + 1
    return "\n".join(lines)


def build_search_tools(
    client: KnowledgeClient,
    run: SearchRun,
    *,
    area_codes: set[str] | None = None,
    default_areas: tuple[str, ...] | None = None,
    max_searches: int = 3,
    result_max_chars: int = 12000,
) -> list[BaseTool]:
    @tool
    async def search_docs(
        query: str, config: RunnableConfig, areas: list[str] | None = None
    ) -> str:
        """Найти пункты по вопросу. areas — коды областей из справочника."""

        if run.attempts >= max_searches:
            return "Лимит поисков исчерпан, отвечай по найденному."
        if not query.strip():
            return "Укажи поисковый запрос."
        allowed_areas = area_codes or set()
        requested = default_areas if default_areas is not None else areas or []
        filters = list(dict.fromkeys(area for area in requested if area in allowed_areas))
        identity = current_identity(config)
        run.attempts += 1
        with events.timed("search.tool", areas=filters, **run_fields(config)) as event:
            try:
                response = await client.search(
                    SearchRequest(
                        query=query.strip(), levels=sorted(identity.levels), areas=filters
                    )
                )
            except KnowledgeUnavailable:
                run.unavailable += 1
                event["status"] = "unavailable"
                return "Поиск временно недоступен."
            event["status"] = "ok" if response.fragments else "empty"
            event["fragments"] = len(response.fragments)
            event["low_confidence"] = response.low_confidence
            event["areas_diverge"] = response.areas_diverge
        run.searches.append((filters, response))
        notes = []
        if response.areas_diverge:
            notes.append("Регламенты расходятся по областям: " + ", ".join(response.areas_found))
        if response.low_confidence or not response.fragments:
            notes.append("Достаточно релевантных пунктов не найдено.")
        return (
            _show_fragments(
                response.fragments, run, result_max_chars, " ".join(notes)[:result_max_chars]
            )
            or "Результаты не помещаются в допустимый размер."
        )

    @tool
    async def read_clause(fragment_id: str, config: RunnableConfig) -> str:
        """Прочитать текст пункта и соседних пунктов по полному id фрагмента."""
        if _PRACTICE_ID.fullmatch(fragment_id):
            return "Текст практики уже в результатах поиска."
        target = parse_fragment_id(fragment_id)
        if target is None:
            return "Пункт не найден."
        doc_id, version, clause = target
        identity = current_identity(config)
        try:
            response = await client.clause(
                ClauseRequest(
                    doc_id=doc_id, version=version, clause=clause, levels=sorted(identity.levels)
                )
            )
        except KnowledgeUnavailable:
            return "Поиск временно недоступен."
        if response is None:
            return "Пункт не найден."
        return (
            _show_fragments([response.fragment, *response.neighbors], run, result_max_chars)
            or "Пункт не помещается в допустимый размер."
        )

    @tool
    async def follow_refs(doc_id: str, config: RunnableConfig) -> str:
        """Связанные регламенты: на какие документы ссылается этот и какие ссылаются на него.
        doc_id — код документа или полный id фрагмента."""
        target = parse_fragment_id(doc_id.strip())
        code = target[0] if target else doc_id.strip()
        if not code or _PRACTICE_ID.fullmatch(code):
            return _NO_REFS
        identity = current_identity(config)
        with events.timed("refs.tool", doc_id=code, **run_fields(config)) as event:
            try:
                response = await client.refs(
                    RefsRequest(doc_id=code, levels=sorted(identity.levels))
                )
            except KnowledgeUnavailable:
                event["status"] = "unavailable"
                return "Поиск временно недоступен."
            if response is None:
                # Документа нет или нет прав — для журнала так же неразличимо, как для модели
                event["status"] = "not_found"
                return _NO_REFS
            event["outgoing"], event["incoming"] = len(response.outgoing), len(response.incoming)
            event["status"] = "ok" if response.outgoing or response.incoming else "empty"
        return _show_refs(response.outgoing, response.incoming, result_max_chars) or _NO_REFS

    return [search_docs, read_clause, follow_refs]
