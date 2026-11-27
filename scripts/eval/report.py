"""Хранение результатов, сводка прогона и очередь разбора провалов."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median, quantiles
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from scripts.eval.checks import Checks, TimedAnswer
from scripts.eval.questions import Question

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CREDENTIAL_KEYS = {
    "password",
    "authorization",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
}


class RunHeader(BaseModel):
    type: Literal["run"] = "run"
    sha: str
    model: str
    stand: str
    started: datetime
    expected: int


class Review(BaseModel):
    semantic_match: bool | None = None
    claims_supported: bool | None = None
    note: str = ""

    @property
    def complete(self) -> bool:
        return self.semantic_match is not None and self.claims_supported is not None


class Diagnosis(BaseModel):
    cause: str
    owner: Literal["knowledge", "backend"]


class CaseResult(BaseModel):
    type: Literal["case"] = "case"
    question: Question
    answer: TimedAnswer | None = None
    checks: Checks | None = None
    error: str | None = None
    error_run_id: UUID | None = None
    review: Review | None = None
    diagnosis: Diagnosis | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks and self.checks.passed) and not (
            self.review
            and (self.review.semantic_match is False or self.review.claims_supported is False)
        )


def _redact(data: Any, secrets: list[str]) -> Any:
    if isinstance(data, str):
        for secret in secrets:
            data = data.replace(secret, "[скрыто]")
        return _JWT.sub("[скрыто]", data)
    if isinstance(data, list):
        return [_redact(item, secrets) for item in data]
    if isinstance(data, dict):
        return {
            _redact(key, secrets): (
                "[скрыто]" if key.lower() in _CREDENTIAL_KEYS else _redact(value, secrets)
            )
            for key, value in data.items()
        }
    return data


def encode_record(record: RunHeader | CaseResult, secrets: set[str] | None = None) -> str:
    values = sorted((value for value in secrets or () if value), key=len, reverse=True)
    data = record.model_dump(mode="json")
    if isinstance(record, RunHeader):
        data["model"] = _redact(data["model"], values)
        data["stand"] = _redact(data["stand"], values)
    else:
        for key in ("question", "reference_answer", "required_phrases", "forbidden_phrases"):
            data["question"][key] = _redact(data["question"][key], values)
        if answer := data["answer"]:
            answer["text"] = _redact(answer["text"], values)
            answer["meta"] = _redact(answer["meta"], values)
        if checks := data["checks"]:
            for key in ("missing_clauses", "leaks", "unverifiable_sources", "behavior_issues"):
                checks[key] = _redact(checks[key], values)
        data["error"] = _redact(data["error"], values)
    return json.dumps(data, ensure_ascii=False) + "\n"


def read_run(path: Path) -> tuple[RunHeader, list[CaseResult]]:
    with path.open(encoding="utf-8") as handle:
        header = RunHeader.model_validate_json(handle.readline())
        return header, [CaseResult.model_validate_json(line) for line in handle if line.strip()]


def _diagnosis(case: CaseResult) -> Diagnosis:
    if case.diagnosis:
        return case.diagnosis
    checks = case.checks
    if checks is None:
        return Diagnosis(cause="выполнение запроса или стрим", owner="backend")
    if checks.leaks:
        return Diagnosis(cause="проверка прав на фрагмент", owner="knowledge")
    if not checks.verified or checks.unverifiable_sources:
        return Diagnosis(cause="проверка ссылок", owner="backend")
    outcome = case.answer.meta.get("outcome") if case.answer else None
    if not checks.outcome and (case.question.outcome == "refusal" or outcome == "refusal"):
        return Diagnosis(cause="guard", owner="backend")
    if not checks.area or (
        not checks.outcome and (case.question.outcome == "ask_area" or outcome == "ask_area")
    ):
        return Diagnosis(cause="определение области или память", owner="backend")
    if checks.missing_clauses:
        return Diagnosis(cause="поиск ожидаемого пункта или выбор источника", owner="knowledge")
    if not checks.timings:
        return Diagnosis(cause="стрим и время ответа", owner="backend")
    return Diagnosis(cause="промпт и формулировка ответа", owner="backend")


def _latency(values: list[float]) -> tuple[str, str]:
    if not values:
        return "—", "—"
    p90 = quantiles(values, n=10, method="inclusive")[8] if len(values) > 1 else values[0]
    return f"{median(values):.3f}", f"{p90:.3f}"


def _block(text: str) -> str:
    width = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, width + 1)
    return f"{fence}text\n{text}\n{fence}"


def _failure(case: CaseResult) -> str:
    diagnosis = _diagnosis(case)
    expected = case.question
    answer = case.answer
    lines = [
        f"### {expected.id}",
        "",
        f"**Учётка** `{expected.user}`",
        f"**Предполагаемая причина** {diagnosis.cause}",
        f"**Владелец** `{diagnosis.owner}`",
        "",
        "**Вопрос**",
        _block(expected.question),
        "",
        "**Эталон**",
        _block(expected.reference_answer),
        "",
        f"**Ожидаемый исход** `{expected.outcome}`",
        f"**Ожидаемые пункты** {', '.join(expected.expected_clauses) or '—'}",
        "",
    ]
    if run_id := (answer.run_id if answer else case.error_run_id):
        lines.extend([f"**ID хода** `{run_id}`", ""])
    if answer:
        lines.extend(
            [
                f"**Полученный исход** `{answer.meta.get('outcome')}`",
                "",
                "**Ответ**",
                _block(answer.text or "Ответ пуст"),
                "",
                "**Источники**",
                _block(json.dumps(answer.meta.get("sources"), ensure_ascii=False)),
                "",
            ]
        )
    if case.error:
        lines.extend([f"**Ошибка** {case.error}", ""])
    if case.checks:
        failed = [label for key, label in _CHECKS.items() if not getattr(case.checks, key)]
        if failed:
            lines.extend([f"**Не прошли проверки** {', '.join(failed)}", ""])
        if case.checks.behavior_issues:
            lines.extend(
                [
                    "**Наблюдения по поведению**",
                    "",
                    *(f"- {issue}" for issue in case.checks.behavior_issues),
                    "",
                ]
            )
    if case.review and case.review.note:
        lines.extend(["**Примечание разметки**", _block(case.review.note), ""])
    return "\n".join(lines)


_CHECKS = {
    "outcome": "Исход",
    "citations": "Пункты и источники",
    "verified": "Признак verified",
    "area": "Область и память",
    "timings": "Оба времени измерены",
    "behavior": "Поведение в диалоге",
}


def render_report(header: RunHeader, cases: list[CaseResult]) -> str:
    reviewed = [case.review for case in cases if case.review and case.review.complete]
    first = [
        case.answer.first_words_s
        for case in cases
        if case.answer and case.answer.first_words_s is not None
    ]
    total = [
        case.answer.total_s for case in cases if case.answer and case.answer.total_s is not None
    ]
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for case in cases:
        groups[case.question.category].append(case)
    leaks = [
        (case.question, case.checks.leaks) for case in cases if case.checks and case.checks.leaks
    ]
    failures = [case for case in cases if not case.passed]
    lines = [
        "# Прогон качества RegAgent",
        "",
        f"- Начало — {header.started.isoformat(timespec='seconds')}",
        f"- SHA локальной ветки `main` — `{header.sha}`",
        f"- Модель из конфигурации — `{header.model}`",
        f"- Стенд — {header.stand}",
        f"- Выполнено — {len(cases)} из {header.expected}",
        f"- Прошли все проверки — {len(cases) - len(failures)} из {len(cases)}",
        "",
        "## Проверки",
        "",
        "| Проверка | Прошли | Всего |",
        "| --- | ---: | ---: |",
    ]
    for key, label in _CHECKS.items():
        passed = sum(bool(case.checks and getattr(case.checks, key)) for case in cases)
        lines.append(f"| {label} | {passed} | {len(cases)} |")
    lines.extend(["", "## Типы вопросов", "", "| Тип | Прошли | Всего |", "| --- | ---: | ---: |"])
    for category, rows in sorted(groups.items()):
        lines.append(f"| {category} | {sum(case.passed for case in rows)} | {len(rows)} |")
    lines.extend(
        [
            "",
            "## Время ответа, секунды",
            "",
            "| Метрика | Медиана | p90 | Замеров |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for label, times in (("До первых слов", first), ("До конца ответа", total)):
        med, p90 = _latency(times)
        lines.append(f"| {label} | {med} | {p90} | {len(times)} |")
    lines.extend(["", "## Утечки", ""])
    if leaks:
        for question, leaked_ids in leaks:
            sources = ", ".join(f"`{source}`" for source in leaked_ids)
            lines.append(f"- `{question.id}`, `{question.user}` — {sources}")
    else:
        lines.append("Среди проверенных источников утечек не найдено.")
    lines.extend(["", "## Ручная разметка", "", f"Размечено ответов — {len(reviewed)} из 20."])
    if reviewed:
        semantic = sum(bool(review.semantic_match) for review in reviewed)
        support = sum(bool(review.claims_supported) for review in reviewed)
        lines.extend(
            [
                f"- Совпали с эталоном по смыслу — {semantic}/{len(reviewed)} "
                f"({semantic / len(reviewed):.1%}).",
                f"- Все утверждения подтверждены — {support}/{len(reviewed)} "
                f"({support / len(reviewed):.1%}).",
            ]
        )
    lines.extend(["", "## Провалы и владельцы", ""])
    lines.extend(_failure(case) for case in failures)
    if not failures:
        lines.append("В выполненных кейсах провалов нет.")
    return "\n".join(lines).rstrip() + "\n"


def write_report(raw: Path, path: Path) -> None:
    header, cases = read_run(raw)
    with NamedTemporaryFile(dir=path.parent, encoding="utf-8", mode="w", delete=False) as handle:
        pending = Path(handle.name)
        try:
            handle.write(render_report(header, cases))
            handle.flush()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
