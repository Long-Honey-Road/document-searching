import pytest

from scripts.eval.checks import TimedAnswer, evaluate
from scripts.eval.questions import Corpus, Question


@pytest.fixture
def question():
    return Question(
        id="case",
        user="employee_public",
        question="Вопрос",
        outcome="answer",
        areas=["ДИБ"],
        expected_clauses=["public-doc#1.2"],
        reference_answer="Эталон",
    )


@pytest.fixture
def corpus():
    return Corpus(
        clauses={"public-doc": frozenset({"1.2"}), "closed-doc": frozenset({"1.1"})},
        levels={"public-doc": "public", "closed-doc": "closed_fin"},
        versions={"public-doc": 1, "closed-doc": 2},
        areas=frozenset({"ДИБ"}),
        accounts={"employee_public": frozenset({"public"})},
    )


def metadata(**overrides):
    return {
        "outcome": "answer",
        "verified": True,
        "area_used": "ДИБ",
        "sources": [{"fragment_id": "public-doc@1#1.2", "clause": "1.2"}],
        **overrides,
    }


def timed(text, meta, first, total):
    return TimedAnswer(text=text, meta=meta, first_words_s=first, total_s=total)


def test_passes_all_checks(question, corpus):
    answer = timed("Норма [f:public-doc@1#1.2]", metadata(), 0.3, 0.5)
    checks = evaluate(question, answer, corpus)
    assert checks.passed
    assert checks.outcome and checks.citations and checks.verified and checks.timings
    assert checks.area and not checks.leaks and not checks.unverifiable_sources


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        ({"outcome": "refusal"}, "outcome"),
        ({"verified": 1}, "verified"),
        ({"sources": []}, "citations"),
        ({"sources": [{"fragment_id": "public-doc@1#1.2", "clause": "9"}]}, "citations"),
        ({"area_used": "общий"}, "area"),
    ],
)
def test_failing_individual_checks(question, corpus, overrides, check):
    result = evaluate(question, timed("Текст", metadata(**overrides), 1, 2), corpus)
    assert not getattr(result, check)
    assert not result.passed and not result.leaks


@pytest.mark.parametrize(
    ("first", "total"), [(None, 2), (1, None), (-1, 2), (2, 1), (0, float("inf"))]
)
def test_rejects_invalid_timings(question, corpus, first, total):
    checks = evaluate(question, timed("Текст", metadata(), first, total), corpus)
    assert not checks.timings
    assert not checks.passed


def test_disallowed_source_is_reported_separately(question, corpus):
    sources = [*metadata()["sources"], {"fragment_id": "closed-doc@2#1.1", "clause": "1.1"}]
    result = evaluate(question, timed("Текст", metadata(sources=sources), 1, 2), corpus)
    assert result.citations and result.verified and result.outcome
    assert result.leaks == ["closed-doc@2#1.1"]
    assert not result.passed


def test_inline_closed_source_is_detected_without_metadata(question, corpus):
    answer = timed("Утверждение [f:closed-doc@2#1.1]", metadata(), 1, 2)
    assert evaluate(question, answer, corpus).leaks == ["closed-doc@2#1.1"]


def test_unrecognized_source_fails_without_claiming_confirmed_leak(question, corpus):
    sources = [*metadata()["sources"], {"fragment_id": "other@1#2", "clause": "2"}]
    result = evaluate(question, timed("Текст", metadata(sources=sources), 1, 2), corpus)
    assert result.unverifiable_sources == ["other@1#2"]
    assert result.leaks == []
    assert not result.passed


def test_new_dialog_checks_whether_area_came_from_memory(question, corpus):
    question = question.model_copy(update={"category": "area", "new_thread": True})
    for source, expected in [("memory", True), ("question", False)]:
        answer = timed("Текст", metadata(area_source=source), 1, 2)
        assert evaluate(question, answer, corpus).area is expected


def test_qc_guidance_requires_action_and_rejects_false_submission_and_sources(question, corpus):
    case = question.model_copy(
        update={
            "outcome": "qc_help",
            "expected_clauses": [],
            "expect_no_sources": True,
            "required_phrases": ["Передать в ОКК", "подтвердите отправку"],
            "forbidden_phrases": ["заявка отправлена"],
        }
    )
    good = timed(
        "Откройте «Передать в ОКК» и подтвердите отправку.",
        metadata(outcome="qc_help", sources=[]),
        1,
        2,
    )
    assert evaluate(case, good, corpus).passed
    bad = timed("Заявка отправлена. [f:public-doc@1#1.2]", metadata(outcome="qc_help"), 1, 2)
    result = evaluate(case, bad, corpus)
    assert not result.passed and not result.behavior
    assert len(result.behavior_issues) == 4
