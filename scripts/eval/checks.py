"""Детерминированная оценка ответа и сверка доступа по манифесту."""

from __future__ import annotations

import re
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field

from scripts.eval.questions import Corpus, Question

_FRAGMENT = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)@([0-9]{1,18})#([^#\s\[\]]+)")
_INLINE = re.compile(r"\[f:([^\s\[\]]+)\]")


class TimedAnswer(BaseModel):
    text: str
    meta: dict[str, Any]
    first_words_s: float | None
    total_s: float | None


class Checks(BaseModel):
    outcome: bool
    citations: bool
    verified: bool
    area: bool
    timings: bool
    missing_clauses: list[str]
    leaks: list[str]
    unverifiable_sources: list[str]
    behavior_issues: list[str] = Field(default_factory=list)

    @property
    def behavior(self) -> bool:
        return not self.behavior_issues

    @property
    def passed(self) -> bool:
        return (
            self.outcome
            and self.citations
            and self.verified
            and self.area
            and self.timings
            and self.behavior
            and not self.leaks
            and not self.unverifiable_sources
        )


def _source_ids(answer: TimedAnswer) -> tuple[dict[str, str], list[str]]:
    sources = answer.meta.get("sources")
    if not isinstance(sources, list):
        return {}, ["Некорректное поле sources"]
    found = {}
    malformed = []
    for index, source in enumerate(sources, 1):
        if not isinstance(source, dict) or not isinstance(source.get("fragment_id"), str):
            malformed.append(f"Источник {index} без fragment_id")
            continue
        clause = source.get("clause")
        found[source["fragment_id"]] = clause if isinstance(clause, str) else ""
    return found, malformed


def _behavior_issues(question: Question, answer: TimedAnswer, has_sources: bool) -> list[str]:
    text = " ".join(answer.text.casefold().split()).replace("ё", "е")
    issues = []
    for phrase in question.required_phrases:
        if " ".join(phrase.casefold().split()).replace("ё", "е") not in text:
            issues.append(f"Нет фразы «{phrase}»")
    for phrase in question.forbidden_phrases:
        if " ".join(phrase.casefold().split()).replace("ё", "е") in text:
            issues.append(f"Недопустимая фраза «{phrase}»")
    if question.expect_no_sources and has_sources:
        issues.append("Получены источники у ответа, для которого они не ожидались")
    return issues


def evaluate(question: Question, answer: TimedAnswer, corpus: Corpus) -> Checks:
    sources, unverifiable = _source_ids(answer)
    inline = set(_INLINE.findall(answer.text))
    allowed = corpus.accounts[question.user]
    leaks = []
    cited = set()
    for fragment_id in sorted(sources.keys() | inline):
        match = _FRAGMENT.fullmatch(fragment_id)
        if not match or match[1] not in corpus.levels:
            unverifiable.append(fragment_id)
            continue
        doc_id, version, clause = match.groups()
        if corpus.levels[doc_id] not in allowed:
            leaks.append(fragment_id)
        if (
            sources.get(fragment_id) != clause
            or int(version) != corpus.versions[doc_id]
            or clause not in corpus.clauses[doc_id]
        ):
            unverifiable.append(fragment_id)
        else:
            cited.add(f"{doc_id}#{clause}")

    missing = sorted(set(question.expected_clauses) - cited)
    area = (
        question.outcome != "answer"
        or not question.areas
        or answer.meta.get("area_used") in question.areas
    )
    if question.category == "area" and question.new_thread and not question.reset_memory:
        area = area and answer.meta.get("area_source") == "memory"
    first, total = answer.first_words_s, answer.total_s
    return Checks(
        outcome=answer.meta.get("outcome") == question.outcome,
        citations=not missing and not unverifiable,
        verified=answer.meta.get("verified") is True,
        area=area,
        timings=first is not None and total is not None and isfinite(total) and 0 <= first <= total,
        missing_clauses=missing,
        leaks=leaks,
        unverifiable_sources=unverifiable,
        behavior_issues=_behavior_issues(question, answer, bool(sources or inline)),
    )
