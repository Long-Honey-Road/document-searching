"""Проверка ссылок и подготовка текста к публикации."""

from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

_CITATION = re.compile(r"\[f:([^\[\]\s]+)\]")
_THINK = re.compile(r"<think\b[^>]*>.*?(?:</think\s*>|\Z)", re.IGNORECASE | re.DOTALL)
_THINK_END = re.compile(r"</think\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class CitationCheck:
    ok: bool
    unknown: list[str]
    has_claims_without_citations: bool


def extract_citations(text: str) -> list[str]:
    return list(dict.fromkeys(_CITATION.findall(text)))


def verify_citations(text: str, seen: set[str]) -> CitationCheck:
    citations = extract_citations(text)
    unknown = [citation for citation in citations if citation not in seen]
    malformed = "[f:" in _CITATION.sub("", text)
    missing = bool(text.strip()) and not citations
    return CitationCheck(bool(citations) and not (unknown or malformed), unknown, missing)


def public_text(message: BaseMessage) -> str:
    text = _THINK.sub("", message.text)
    stray_end = list(_THINK_END.finditer(text))
    if stray_end:
        text = text[stray_end[-1].end() :]
    return text.strip()
