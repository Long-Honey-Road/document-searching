"""Проверка ссылок и подготовка текста к публикации."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, StrictBool
from regagent_common.contracts import Fragment

from regagent_backend.guard import PROMPTS, StructuredMode, invoke_structured

_CITATION = re.compile(r"\[f:([^\[\]\s]+)\]")
_THINK = re.compile(r"<think\b[^>]*>.*?(?:</think\s*>|\Z)", re.IGNORECASE | re.DOTALL)
_THINK_END = re.compile(r"</think\s*>", re.IGNORECASE)
_UNCERTAIN = re.compile(
    r"\b(?:подтвердить|определить|установить|однозначно ответить)"
    r"[^.!?\n]{0,130}\b(?:не могу|нельзя|не удалось|невозможно)\b"
    r"|\b(?:не удалось|невозможно|нельзя|не могу|не может)\s+(?:однозначно\s+)?"
    r"(?:подтвердить|определить|установить|ответить)\b"
    r"|\bдля\s+(?:однозначного\s+)?ответа\s+(?:не хватает|недостаточно)"
    r"\s+(?:данных|сведений)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CitationCheck:
    ok: bool
    unknown: list[str]
    has_claims_without_citations: bool
    malformed: bool


class ComparisonSupport(BaseModel):
    supported: StrictBool


class EvidenceReview(BaseModel):
    outcome: Literal["answer", "not_found", "ask_clarification"]
    question: str | None


def needs_evidence_review(text: str) -> bool:
    return bool(_UNCERTAIN.search(text.replace("ё", "е")))


def extract_citations(text: str) -> list[str]:
    return list(dict.fromkeys(_CITATION.findall(text)))


def verify_citations(text: str, seen: set[str]) -> CitationCheck:
    citations = extract_citations(text)
    unknown = [citation for citation in citations if citation not in seen]
    malformed = "[f:" in _CITATION.sub("", text)
    missing = bool(text.strip()) and not citations
    return CitationCheck(
        bool(citations) and not (unknown or malformed), unknown, missing, malformed
    )


def missing_overrides(text: str, fragments: Mapping[str, Fragment]) -> list[str]:
    """Практика ``overrides`` из этого запуска, привязанная к процитированному пункту, но не
    процитированная сама. Ответ по заменённому пункту без неё вводит в заблуждение."""
    cited = set(extract_citations(text))
    targets = {
        (fragment.doc_id, fragment.version, fragment.clause)
        for fragment_id, fragment in fragments.items()
        if fragment_id in cited and fragment.kind == "regulation"
    }
    missing = []
    for fragment_id, fragment in fragments.items():
        source = fragment.practice
        if fragment.kind != "practice" or source is None or source.mode != "overrides":
            continue
        bound = {(b.doc_id, b.version, b.clause) for b in source.binds}
        if bound & targets and fragment_id not in cited:
            missing.append(fragment_id)
    return missing


def _source_data(fragments: list[Fragment]) -> list[dict[str, str]]:
    return [
        {
            "id": fragment.fragment_id,
            "область": fragment.area,
            "документ": fragment.title,
            "текст": fragment.text,
        }
        for fragment in fragments
    ]


async def review_evidence(
    question: str,
    text: str,
    cited: list[Fragment],
    model: BaseChatModel,
    mode: StructuredMode,
    run: dict[str, str],
) -> EvidenceReview:
    return await invoke_structured(
        model,
        EvidenceReview,
        mode,
        (PROMPTS / "evidence_check.md").read_text(encoding="utf-8"),
        {"вопрос": question, "ответ": text, "источники": _source_data(cited)},
        {"llm_operation": "evidence_check", **run},
    )


async def verify_comparison(
    text: str,
    cited: list[Fragment],
    model: BaseChatModel,
    mode: StructuredMode,
    run: dict[str, str] | None = None,
) -> bool:
    result = await invoke_structured(
        model,
        ComparisonSupport,
        mode,
        (PROMPTS / "comparison_check.md").read_text(encoding="utf-8"),
        {"ответ": text, "источники": _source_data(cited)},
        {"llm_operation": "comparison_check", **(run or {})},
    )
    return result.supported


def public_text(message: BaseMessage) -> str:
    text = _THINK.sub("", message.text)
    stray_end = list(_THINK_END.finditer(text))
    if stray_end:
        text = text[stray_end[-1].end() :]
    return text.strip()
