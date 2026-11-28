"""Поиск в разрешённых workspace и повторная проверка прав по реестру."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from itertools import islice, pairwise, zip_longest
from time import monotonic
from typing import Protocol

from regagent_common.contracts import Fragment, PracticeSource, SearchRequest, SearchResponse
from regagent_common.fragments import FragmentHeader, fragment_id, parse_header

from .manifest import COMMON_AREA
from .normalize import Clause, split_clauses
from .registry import DocumentRecord
from .rerank import Reranker, search_tokens
from .settings import (
    DEFAULT_RERANK_CANDIDATE_LIMIT,
    DEFAULT_RERANK_TEXT_MAX_CHARS,
    DEFAULT_SEARCH_TOP_K,
)

log = logging.getLogger(__name__)
_PROJECT_LEAD = re.compile(r"\bРП\b")
_PROJECT_LEAD_FULL = re.compile(r"\bруководител\w*\s+проект\w*\b", re.I)


class ChunkSearch(Protocol):
    @property
    def levels(self) -> list[str]: ...

    async def query(self, level: str, query: str, top_k: int) -> list[str]: ...


def _fragment(header: FragmentHeader, raw: str) -> Fragment:
    content = raw.partition("⟧")[2].strip()
    title = header.doc_id
    if not header.is_practice:
        first_line = content.partition("\n")[0]
        separator = re.search(rf" — (?={re.escape(header.clause)}(?:\s|$))", first_line)
        if separator is not None:
            title = content[: separator.start()].strip()
            content = content[separator.end() :]
    return Fragment(
        fragment_id=fragment_id(header),
        doc_id=header.doc_id,
        version=header.version,
        title=title,
        clause=header.clause,
        area=header.area,
        kind="practice" if header.is_practice else "regulation",
        text=content,
        score=0,
    )


def _exact_clauses(
    header: FragmentHeader,
    section: Fragment,
    query_terms: set[str],
    query_pairs: set[tuple[str, str]],
    clauses: list[Clause],
    *,
    only_subclauses: bool,
) -> list[Fragment]:
    if header.is_practice or not query_terms:
        return []
    passages = []
    frequencies: Counter[str] = Counter()
    prefix = header.clause + "."
    for index, clause in enumerate(clauses):
        if only_subclauses and not clause.number.startswith(prefix):
            continue
        text = clause.text
        if index + 1 < len(clauses) and clauses[index + 1].number.startswith(clause.number + "."):
            text = text.partition(clauses[index + 1].text)[0].strip()
            if "\n" not in text:
                continue
        words = search_tokens(text)
        terms = set(words)
        frequencies.update(terms)
        if matching := query_terms & terms:
            phrase_matches = len(query_pairs.intersection(pairwise(words)))
            passages.append((clause.number, text, matching, len(terms), phrase_matches))
    weights = {
        word: math.log1p(len(clauses) / frequencies[word])
        for word in query_terms
        if frequencies[word]
    }
    passages.sort(
        key=lambda passage: (
            (sum(weights[word] for word in passage[2]) + 4 * passage[4]) / passage[3] ** 0.2
        ),
        reverse=True,
    )
    limit = 2 if only_subclauses else 8
    return [
        section.model_copy(
            update={
                "fragment_id": fragment_id(replace(header, clause=number)),
                "clause": number,
                "text": text,
            }
        )
        for number, text, _, _, _ in passages[:limit]
    ]


def _balanced_candidates(fragments: list[Fragment], limit: int) -> list[Fragment]:
    if len(fragments) <= limit:
        return fragments
    by_document: dict[str, list[Fragment]] = {}
    for fragment in fragments:
        by_document.setdefault(fragment.doc_id, []).append(fragment)
    candidates = (
        fragment
        for round_ in zip_longest(*by_document.values())
        for fragment in round_
        if fragment is not None
    )
    return list(islice(candidates, limit))


def _rerank_excerpt(text: str, query_terms: set[str], limit: int) -> str:
    if len(text) <= limit:
        return text
    windows = (text[i : i + limit] for i in range(0, len(text), max(1, limit // 2)))
    return max(windows, key=lambda excerpt: len(query_terms.intersection(search_tokens(excerpt))))


def _matches_access(header: FragmentHeader, access: DocumentRecord | str | None) -> bool:
    if isinstance(access, DocumentRecord):
        return (
            access.doc_id == header.doc_id
            and access.status == "indexed"
            and access.level == header.level
            and access.version is not None
            and access.version == header.version
        )
    return access == header.level


async def search(
    req: SearchRequest,
    rag: ChunkSearch,
    levels_lookup: Callable[[str], Awaitable[DocumentRecord | str | None]],
    reranker: Reranker,
    min_score: float,
    *,
    default_top_k: int = DEFAULT_SEARCH_TOP_K,
    registry_snapshot: Callable[[], Awaitable[list[DocumentRecord]]] | None = None,
    indexed_clauses: Callable[[DocumentRecord], Awaitable[list[Clause]]] | None = None,
    practice_lookup: Callable[[FragmentHeader, str], Awaitable[PracticeSource | None]]
    | None = None,
    rerank_candidate_limit: int = DEFAULT_RERANK_CANDIDATE_LIMIT,
    rerank_text_max_chars: int = DEFAULT_RERANK_TEXT_MAX_CHARS,
) -> SearchResponse:
    top_k = req.top_k if req.top_k is not None else default_top_k
    known = set(rag.levels)
    levels = [level for level in dict.fromkeys(req.levels) if level in known]
    if not levels:
        return SearchResponse(
            fragments=[], areas_found=[], areas_diverge=False, low_confidence=False
        )

    started = monotonic()
    # Заголовки фрагментов не содержат хэш. Читаем реестр до поиска, чтобы смена
    # содержимого при том же номере редакции стала видна при повторной сверке.
    snapshot = (
        {row.doc_id: row for row in await registry_snapshot() if row.status == "indexed"}
        if registry_snapshot is not None
        else None
    )
    query = req.query
    if _PROJECT_LEAD.search(query) and not _PROJECT_LEAD_FULL.search(query):
        query += " (РП — руководитель проекта)"
    chunks_by_level = await asyncio.gather(*(rag.query(level, query, top_k) for level in levels))
    allowed_areas = {*req.areas, COMMON_AREA} if req.areas else None
    acl: dict[str, DocumentRecord | str | None] = {}
    candidates: dict[tuple[str, str, str], Fragment] = {}
    practice_chunks: dict[tuple[str, str], tuple[FragmentHeader, str]] = {}
    retrieved_docs: set[str] = set()
    query_words = search_tokens(query)
    query_terms = set(query_words)
    query_pairs = set(pairwise(query_words))
    for level, chunks in zip(levels, chunks_by_level, strict=True):
        for raw in chunks:
            header = parse_header(raw)
            if header is None or header.level != level:
                log.warning("security: неверный заголовок фрагмента в workspace %s", level)
                continue
            if header.doc_id not in acl:
                acl[header.doc_id] = await levels_lookup(header.doc_id)
            access = acl[header.doc_id]
            if not _matches_access(header, access):
                log.warning("security: уровень или версия фрагмента расходятся с реестром")
                continue
            if snapshot is not None and snapshot.get(header.doc_id) != access:
                log.warning("security: запись реестра изменилась во время запроса к индексу")
                continue
            if allowed_areas is not None and header.area not in allowed_areas:
                continue
            section = _fragment(header, raw)
            if header.is_practice:
                practice = (
                    await practice_lookup(header, raw) if practice_lookup is not None else None
                )
                if practice is None:
                    continue
                section.practice = practice
                section.title = (
                    f"Пояснение к регламенту «{access.title or header.doc_id}»"
                    if (isinstance(access, DocumentRecord))
                    else "Пояснение по практике"
                )
                section.version = None
                practice_chunks[section.fragment_id, section.area] = (header, raw)
            exact: list[Fragment] = []
            if not header.is_practice:
                if indexed_clauses is not None and isinstance(access, DocumentRecord):
                    if header.doc_id not in retrieved_docs:
                        retrieved_docs.add(header.doc_id)
                        exact = _exact_clauses(
                            header,
                            section,
                            query_terms,
                            query_pairs,
                            await indexed_clauses(access),
                            only_subclauses=False,
                        )
                else:
                    exact = _exact_clauses(
                        header,
                        section,
                        query_terms,
                        query_pairs,
                        split_clauses(section.text),
                        only_subclauses=True,
                    )
            for fragment in [*exact, section]:
                key = (fragment.fragment_id, fragment.area, fragment.text)
                candidates.setdefault(key, fragment)

    fragments = _balanced_candidates(list(candidates.values()), rerank_candidate_limit)
    texts = [
        _rerank_excerpt(fragment.text, query_terms, rerank_text_max_chars) for fragment in fragments
    ]
    retrieved = monotonic()
    scores = await reranker.rerank(query, texts) if texts else []
    log.info(
        "search retrieval_s=%.2f rerank_s=%.2f candidates=%d",
        retrieved - started,
        monotonic() - retrieved,
        len(texts),
    )
    if len(scores) != len(fragments) or any(not math.isfinite(score) for score in scores):
        raise ValueError("реранкер вернул некорректные оценки")

    scored = [
        (fragment, score)
        for fragment, score in zip(fragments, scores, strict=True)
        if score >= min_score
    ]
    doc_ids = list(dict.fromkeys(fragment.doc_id for fragment, _ in scored))
    updated = await asyncio.gather(*(levels_lookup(doc_id) for doc_id in doc_ids))
    current_acl = dict(zip(doc_ids, updated, strict=True))
    confident = []
    anchors: dict[str, Fragment] = {}
    for fragment, score in scored:
        if current_acl[fragment.doc_id] != acl[fragment.doc_id]:
            log.warning("security: запись реестра изменилась во время реранка")
            continue
        if fragment.practice is not None and practice_lookup is not None:
            header, raw = practice_chunks[fragment.fragment_id, fragment.area]
            if await practice_lookup(header, raw) != fragment.practice:
                continue
            record = current_acl[fragment.doc_id]
            if indexed_clauses is not None and isinstance(record, DocumentRecord):
                clauses = await indexed_clauses(record)
                clause = next((part for part in clauses if part.number == header.clause), None)
                if clause is None:
                    continue
                anchor_id = fragment_id(replace(header, practice_id=None, mode=None))
                anchors[fragment.fragment_id] = Fragment(
                    fragment_id=anchor_id,
                    doc_id=header.doc_id,
                    version=header.version,
                    title=record.title or header.doc_id,
                    clause=header.clause,
                    area=header.area,
                    kind="regulation",
                    text=clause.text,
                    score=score,
                )
        fragment.score = score
        confident.append(fragment)
    confident.sort(key=lambda fragment: fragment.score, reverse=True)
    areas = {fragment.area for fragment in confident}
    unique: dict[str, Fragment] = {}
    for fragment in confident:
        unique.setdefault(fragment.fragment_id, fragment)

    selected: dict[str, Fragment] = {}
    for fragment in unique.values():
        if len(selected) >= top_k:
            break
        selected.setdefault(fragment.fragment_id, fragment)
        if len(selected) < top_k and (anchor := anchors.get(fragment.fragment_id)) is not None:
            selected.setdefault(anchor.fragment_id, anchor)

    return SearchResponse(
        fragments=list(selected.values()),
        areas_found=sorted(areas),
        areas_diverge=not req.areas and len(areas) > 1,
        low_confidence=bool(fragments) and not scored,
    )
