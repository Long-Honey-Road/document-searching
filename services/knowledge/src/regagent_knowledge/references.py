"""Сопоставление упомянутых названий с манифестом корпуса."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache

import pymorphy3

from .manifest import ManifestEntry
from .normalize import Clause

_WORD = re.compile(r"[^\W_]+")
_SENTENCE_END = re.compile(r"[.!?;]")
_DESIGNATOR = r"(?:регламент\w*|инструкци\w*|положени\w*|методик\w*)"
_EXPLICIT = re.compile(
    rf"(?:\b(?:согласно|в\s+соответствии\s+с|описан\w*\s+в|установлен\w*\s+в|см\.)"
    rf"\s+(?:внутренн\w*\s+)?[«\"*_]*|[«\"])(?P<title>{_DESIGNATOR}\s+[^\n.!?;:«»\"*]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Link:
    doc_id: str
    clause: str
    via: str


@dataclass(frozen=True)
class UnresolvedLink:
    clause: str
    via: str


@dataclass(frozen=True)
class AmbiguousLink(UnresolvedLink):
    candidates: list[str]


@dataclass
class DocumentReferences:
    links: list[Link] = field(default_factory=list)
    needs_review: list[AmbiguousLink] = field(default_factory=list)
    missing: list[UnresolvedLink] = field(default_factory=list)


@cache
def _morphology() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=16384)
def _lemma(word: str) -> str:
    if word.isascii():
        return word
    candidates = _morphology().parse(word)
    return (candidates[0].normal_form if candidates else word).replace("ё", "е")


def _lemmas(text: str) -> tuple[str, ...]:
    return tuple(_lemma(word.group().casefold()) for word in _WORD.finditer(text))


def _own_clauses(clauses: Sequence[Clause]) -> Iterator[tuple[str, str]]:
    for index, clause in enumerate(clauses):
        child = clauses[index + 1] if index + 1 < len(clauses) else None
        if child is not None and child.number.startswith(clause.number + "."):
            yield clause.number, clause.text.partition("\n" + child.text.splitlines()[0])[0]
        else:
            yield clause.number, clause.text


def _matches(
    text: str, by_first: dict[str, list[tuple[str, ...]]]
) -> Iterator[tuple[tuple[str, ...], int, int]]:
    words = list(_WORD.finditer(text))
    lemmas = tuple(_lemma(word.group().casefold()) for word in words)
    skip_to = 0
    for index, first in enumerate(lemmas):
        if index < skip_to:
            continue
        for title in by_first.get(first, []):
            end = index + len(title)
            if lemmas[index:end] != title:
                continue
            start_char, end_char = words[index].start(), words[end - 1].end()
            if _SENTENCE_END.search(text[start_char:end_char]):
                continue
            skip_to = end
            yield title, start_char, end_char
            break


def find_document_references(
    source_id: str, clauses: Sequence[Clause], manifest: Iterable[ManifestEntry]
) -> DocumentReferences:
    catalog: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for entry in manifest:
        title = _lemmas(entry.title)
        if len(title) > 1:
            catalog[title].add(entry.doc_id)
    by_first: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for title in sorted(catalog, key=len, reverse=True):
        by_first[title[0]].append(title)

    result = DocumentReferences()
    for number, text in _own_clauses(clauses):
        matched: set[tuple[str, ...]] = set()
        spans: list[tuple[int, int]] = []
        for key, start, end in _matches(text, by_first):
            spans.append((start, end))
            if key in matched:
                continue
            matched.add(key)
            ids = sorted(catalog[key])
            if len(ids) > 1:
                result.needs_review.append(AmbiguousLink(number, text[start:end], ids))
            elif ids[0] != source_id:
                result.links.append(Link(ids[0], number, text[start:end]))

        for mention in _EXPLICIT.finditer(text):
            start, end = mention.span("title")
            if any(start <= left < end for left, _ in spans):
                continue
            via = mention["title"].strip()
            key = _lemmas(via)
            if len(key) > 1 and key not in matched:
                matched.add(key)
                result.missing.append(UnresolvedLink(number, via))
    return result
