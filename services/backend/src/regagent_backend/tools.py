"""Инструменты поиска. Контекст полученных фрагментов живёт только в текущем запуске."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from regagent_common.contracts import ClauseRequest, Fragment, SearchRequest, SearchResponse

from regagent_backend.identity import current_identity
from regagent_backend.knowledge_client import KnowledgeClient, KnowledgeUnavailable

_REGULATION_ID = re.compile(r"(?P<doc>[^\s#]+)@(?P<version>[0-9]{1,18})#(?P<clause>[^\s#]+)")
_PRACTICE_ID = re.compile(r"practice:[^\s#]+")


@dataclass
class SearchRun:
    fragments: dict[str, Fragment] = field(default_factory=dict)
    searches: list[tuple[list[str], SearchResponse]] = field(default_factory=list)
    attempts: int = 0


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
        try:
            response = await client.search(
                SearchRequest(query=query.strip(), levels=sorted(identity.levels), areas=filters)
            )
        except KnowledgeUnavailable:
            return "Поиск временно недоступен."
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

    return [search_docs, read_clause]
