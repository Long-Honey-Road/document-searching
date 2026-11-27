"""Разбиение markdown на доступные по номеру пункты и фрагменты LightRAG."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from lightrag.utils import TiktokenTokenizer, Tokenizer
from regagent_common.fragments import FragmentHeader, format_header

from .manifest import ManifestEntry

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+)$")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)(?:[.)])?(?:\*\*|__)?(?:[ \t]+(.*))?$")


@dataclass(frozen=True)
class Clause:
    number: str
    title: str
    text: str
    depth: int


@dataclass(frozen=True)
class Chunk:
    header: FragmentHeader
    text: str


def _numbered(line: str) -> tuple[str, str] | None:
    if heading := _HEADING.match(line):
        line = heading[2]
    if line.startswith(("**", "__")):
        line = line[2:]
    match = _NUMBERED.fullmatch(line)
    if not match:
        return None
    return match[1], (match[2] or "").removesuffix("**").removesuffix("__").strip()


def _contains(parent: str, child: str) -> bool:
    return child.startswith(parent + ".")


def split_clauses(markdown: str) -> list[Clause]:
    lines = markdown.strip().splitlines()
    numbered = [
        (i, number, title, number.count(".") + 1)
        for i, line in enumerate(lines)
        if (marker := _numbered(line.strip())) is not None
        for number, title in [marker]
    ]
    if not numbered:
        headings = [
            (i, title.rstrip("#").strip(), len(mark))
            for i, line in enumerate(lines)
            if (heading := _HEADING.fullmatch(line.strip())) is not None
            for mark, title in [heading.groups()]
        ]
        if not lines:
            return []
        if not headings or headings[0][0] > 0:
            headings.insert(0, (0, "", 1))
        stops = [start for start, _, _ in headings[1:]] + [len(lines)]
        return [
            Clause(
                f"h{index + 1}",
                title,
                "\n".join(lines[start:stop]).strip(),
                depth,
            )
            for index, ((start, title, depth), stop) in enumerate(zip(headings, stops, strict=True))
        ]

    if numbered[0][0] > 0:
        numbered.insert(0, (0, "h0", "", 1))
    clauses = []
    for index, (start, number, title, depth) in enumerate(numbered):
        end = index + 1
        while end < len(numbered) and _contains(number, numbered[end][1]):
            end += 1
        stop = numbered[end][0] if end < len(numbered) else len(lines)
        clauses.append(Clause(number, title, "\n".join(lines[start:stop]).strip(), depth))
    return clauses


def _clause_end(clauses: list[Clause], start: int) -> int:
    end = start + 1
    while end < len(clauses) and _contains(clauses[start].number, clauses[end].number):
        end += 1
    return end


def render_markdown(clauses: list[Clause]) -> str:
    blocks = []
    start = 0
    while start < len(clauses):
        blocks.append(clauses[start].text)
        start = _clause_end(clauses, start)
    return "\n\n".join(blocks)


@cache
def _default_tokenizer() -> Tokenizer:
    return TiktokenTokenizer()


def build_chunks(
    entry: ManifestEntry,
    clauses: list[Clause],
    max_tokens: int,
    *,
    tokenizer: Tokenizer | None = None,
) -> list[Chunk]:
    tokenizer = tokenizer or _default_tokenizer()
    chunks: list[Chunk] = []

    def chunk_for(clause: Clause, text: str) -> Chunk:
        header = FragmentHeader(
            doc_id=entry.doc_id,
            version=entry.version,
            clause=clause.number,
            area=entry.areas[0],
            level=entry.access,
        )
        title = f"{entry.title} — {clause.number} {clause.title}".rstrip()
        return Chunk(header, f"{format_header(header)} {title}\n{text}".rstrip())

    def append_sections(start: int, stop: int) -> None:
        while start < stop:
            clause = clauses[start]
            end = _clause_end(clauses, start)
            first_line, _, body = clause.text.partition("\n")
            if clause.title and (
                _numbered(first_line.strip()) is not None or _HEADING.match(first_line.strip())
            ):
                text = body.lstrip()
            else:
                text = clause.text
            chunk = chunk_for(clause, text)
            if len(tokenizer.encode(chunk.text)) <= max_tokens:
                chunks.append(chunk)
            elif start + 1 < end:
                intro = text.partition(clauses[start + 1].text)[0].strip()
                if intro:
                    introductory_chunk = chunk_for(clause, intro)
                    if len(tokenizer.encode(introductory_chunk.text)) > max_tokens:
                        raise ValueError(
                            f"пункт {clause.number} превышает предел {max_tokens} токенов"
                        )
                    chunks.append(introductory_chunk)
                append_sections(start + 1, end)
            else:
                raise ValueError(f"пункт {clause.number} превышает предел {max_tokens} токенов")
            start = end

    append_sections(0, len(clauses))
    return chunks
