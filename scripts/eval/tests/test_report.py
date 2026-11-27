from datetime import UTC, datetime

from scripts.eval.checks import Checks, TimedAnswer
from scripts.eval.questions import Question
from scripts.eval.report import (
    CaseResult,
    RunHeader,
    encode_record,
    read_run,
    render_report,
)


def result(case_id, first, total, *, outcome="answer", leaks=None, review=None):
    question = Question(
        id=case_id,
        user="employee_public",
        question="Что говорит регламент?",
        areas=[],
        outcome="answer",
        expected_clauses=["reg#1.2"],
        reference_answer="Эталон",
    )
    answer = TimedAnswer(
        text="Полученный ответ",
        meta={"outcome": outcome, "sources": [{"fragment_id": "reg@1#1.2"}]},
        first_words_s=first,
        total_s=total,
    )
    checks = Checks(
        outcome=outcome == "answer",
        citations=True,
        verified=True,
        area=True,
        timings=True,
        missing_clauses=[],
        leaks=leaks or [],
        unverifiable_sources=[],
    )
    return CaseResult(question=question, answer=answer, checks=checks, review=review)


def header(expected):
    return RunHeader(
        sha="abc123",
        model="model",
        stand="http://localhost:8080",
        started=datetime(2026, 11, 27, tzinfo=UTC),
        expected=expected,
    )


def test_report_includes_scores_latencies_review_leaks_failure_and_owner():
    cases = [
        result("first", 1, 2, review={"semantic_match": True, "claims_supported": True}),
        result("second", 3, 4, outcome="refusal", leaks=["closed@1#2"]),
    ]
    report = render_report(header(3), cases)
    assert "2 из 3" in report
    assert "| До первых слов | 2.000 | 2.800 |" in report
    assert "| До конца ответа | 3.000 | 3.800 |" in report
    assert "100.0%" in report
    assert "closed@1#2" in report and "## Утечки" in report
    assert "knowledge" in report
    assert "Полученный ответ" in report and "reg@1#1.2" in report


def test_read_run_preserves_review_and_redacts_credentials_at_write(tmp_path):
    raw = tmp_path / "run.jsonl"
    run = header(1)
    case = result("first", 1, 2, review={"semantic_match": False, "claims_supported": True})
    assert case.answer is not None
    case.answer.text = "secret / another-secret"
    case.answer.meta["details"] = "eyJ0eXAiOiJKV1QifQ.eyJzdWIiOiJ1In0.signature"
    raw.write_text(encode_record(run) + encode_record(case, {"secret", "another-secret"}))
    record, [row] = read_run(raw)
    assert record == run
    assert row.review is not None and row.review.semantic_match is False
    assert row.answer is not None and row.answer.text == "[скрыто] / [скрыто]"
    assert "secret" not in raw.read_text() and "eyJ" not in raw.read_text()


def test_report_counts_a_semantic_failure_even_when_automatic_checks_pass():
    cases = [result("first", 1, 2, review={"semantic_match": False, "claims_supported": True})]
    report = render_report(header(1), cases)
    assert "0.0%" in report and "### first" in report
    assert "промпт" in report and "backend" in report
