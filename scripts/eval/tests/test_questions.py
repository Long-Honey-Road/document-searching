from pathlib import Path

import pytest
import yaml

from scripts.eval.questions import Corpus, load_questions, read_corpus
from scripts.synth.generate import build_corpus

ROOT = Path(__file__).resolve().parents[3]


def test_legacy_case_defaults(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump([case()]))
    [question] = load_questions(path, corpus())
    assert question.scenario is None
    assert not question.new_thread
    assert not question.reset_memory
    assert question.category == "simple"


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"expected_clauses": ["missing#1.2"]}, "неизвестный документ"),
        ({"expected_clauses": ["reg#1.3"]}, "нет пункта"),
        ({"expected_clauses": ["reg@1#1.2"]}, "doc_id#пункт"),
        ({"user": "unknown"}, "employee_public"),
        ({"outcome": "unknown"}, "not_found"),
        ({"areas": ["wrong"]}, "неизвестная область"),
        ({"user": "employee_fin"}, "нет доступа"),
        ({"new_thread": True}, "требует scenario"),
    ],
)
def test_rejects_invalid_cases(tmp_path, updates, error):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump([case(**updates)]))
    with pytest.raises(ValueError, match=error):
        load_questions(path, corpus())


def test_rejects_duplicate_ids_and_scenario_without_initial_reset(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(yaml.safe_dump([case(), case()]))
    with pytest.raises(ValueError, match="повторяющийся id"):
        load_questions(path, corpus())
    path.write_text(yaml.safe_dump([case(scenario="two-turns")]))
    with pytest.raises(ValueError, match="начинаться со сброса"):
        load_questions(path, corpus())


def test_repo_questions_match_normalized_corpus_and_cover_the_plan(tmp_path):
    from collections import Counter

    from scripts.eval.questions import account_levels

    manifest = build_corpus(ROOT / "data/samples", tmp_path)
    access, accounts = account_levels(ROOT)
    loaded = read_corpus(manifest, access, accounts)
    questions = load_questions(ROOT / "eval/questions.yaml", loaded)
    selected = [question for question in questions if question.category != "revision"]
    assert 30 <= len(selected) <= 40
    counts = Counter(question.category for question in selected)
    assert 8 <= counts["simple"] <= 10
    assert 4 <= counts["multi_doc"] <= 6
    assert 3 <= counts["not_found"] <= 4
    assert 3 <= counts["refusal"] <= 4
    scenarios = {question.scenario for question in selected if question.category == "area"}
    assert 3 <= len(scenarios) <= 4
    for scenario in scenarios:
        steps = [question for question in selected if question.scenario == scenario]
        assert [step.outcome for step in steps] == ["ask_area", "answer", "answer"]
        assert steps[0].reset_memory and not steps[1].new_thread and steps[2].new_thread
    closed = [question for question in selected if question.category == "closed"]
    pairs = {question.question for question in closed}
    assert 3 <= len(pairs) <= 4
    for question in pairs:
        assert {(case.user, case.outcome) for case in closed if case.question == question} == {
            ("employee_public", "not_found"),
            ("employee_fin", "answer"),
        }


def case(**updates):
    return {
        "id": "case",
        "user": "employee_public",
        "question": "Каков порядок?",
        "outcome": "answer",
        "areas": ["общий"],
        "expected_clauses": ["reg#1.2"],
        "reference_answer": "Эталон.",
        **updates,
    }


def corpus():
    return Corpus(
        clauses={"reg": frozenset({"1.2"})},
        levels={"reg": "public"},
        versions={"reg": 1},
        areas=frozenset({"общий"}),
        accounts={"employee_public": frozenset({"public"}), "employee_fin": frozenset()},
    )
