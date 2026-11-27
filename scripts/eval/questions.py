"""Формат вопросов и сверка с документами, которые будет использовать стенд."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter
from regagent_common.access import AccessLevels
from regagent_knowledge.manifest import load_manifest

Account = Literal["employee_public", "employee_fin"]
Outcome = Literal["answer", "refusal", "not_found", "ask_area", "fallback"]
Category = Literal["simple", "multi_doc", "area", "closed", "not_found", "refusal", "revision"]
Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Identifier
    user: Account
    question: Nonempty
    outcome: Outcome
    areas: list[str]
    expected_clauses: list[str]
    reference_answer: Nonempty
    category: Category = "simple"
    scenario: Identifier | None = None
    new_thread: bool = False
    reset_memory: bool = False


@dataclass(frozen=True)
class Corpus:
    clauses: dict[str, frozenset[str]]
    levels: dict[str, str]
    versions: dict[str, int]
    areas: frozenset[str]
    accounts: dict[str, frozenset[str]]


def account_levels(root: Path) -> tuple[AccessLevels, dict[str, frozenset[str]]]:
    levels = AccessLevels.from_yaml(root / "config/access_levels.yaml")
    realm = json.loads((root / "deploy/keycloak/regagent-realm.json").read_text(encoding="utf-8"))
    groups = {entry["username"]: entry.get("groups", []) for entry in realm["users"]}
    return levels, {
        user: levels.levels_for(groups[user]) for user in ("employee_public", "employee_fin")
    }


def read_corpus(
    manifest: Path, levels: AccessLevels, accounts: dict[str, frozenset[str]]
) -> Corpus:
    from regagent_knowledge.normalize import split_clauses
    from regagent_knowledge.parsing import DoclingParser

    loaded = load_manifest(manifest, levels)
    if loaded.errors:
        errors = "\n".join(
            f"{error.doc_id or manifest}: {error.message}" for error in loaded.errors
        )
        raise ValueError(f"Ошибки манифеста:\n{errors}")
    if not loaded.valid:
        raise ValueError(f"{manifest}: манифест не содержит документов")

    parser = DoclingParser()
    clauses = {}
    for entry in loaded.valid:
        path = manifest.parent / entry.file
        if not path.is_file():
            raise ValueError(f"{entry.doc_id}: файл {path} отсутствует")
        clauses[entry.doc_id] = frozenset(
            clause.number for clause in split_clauses(parser.parse(path))
        )

    return Corpus(
        clauses=clauses,
        levels={entry.doc_id: entry.access for entry in loaded.valid},
        versions={entry.doc_id: entry.version for entry in loaded.valid},
        areas=frozenset(loaded.area_codes),
        accounts=accounts,
    )


def _check_reference(reference: str, question: Question, corpus: Corpus) -> None:
    doc_id, separator, clause = reference.partition("#")
    if not (doc_id and separator and clause) or "@" in doc_id or "#" in clause:
        raise ValueError(f"{question.id}: ссылка {reference!r} должна иметь формат doc_id#пункт")
    if doc_id not in corpus.clauses:
        raise ValueError(f"{question.id}: неизвестный документ {doc_id}")
    if clause not in corpus.clauses[doc_id]:
        raise ValueError(f"{question.id}: в документе {doc_id} нет пункта {clause}")
    if corpus.levels[doc_id] not in corpus.accounts[question.user]:
        raise ValueError(f"{question.id}: у {question.user} нет доступа к {doc_id}")


def load_questions(path: Path, corpus: Corpus) -> list[Question]:
    questions = TypeAdapter(list[Question]).validate_python(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if not questions:
        raise ValueError(f"{path}: набор вопросов пуст")

    ids = set()
    scenarios = set()
    for question in questions:
        if question.id in ids:
            raise ValueError(f"{question.id}: повторяющийся id")
        ids.add(question.id)
        if question.new_thread and not question.scenario:
            raise ValueError(f"{question.id}: new_thread требует scenario")
        if question.scenario:
            scenario = (question.user, question.scenario)
            if scenario not in scenarios and not question.reset_memory:
                raise ValueError(f"{question.id}: сценарий должен начинаться со сброса памяти")
            scenarios.add(scenario)
        if unknown := set(question.areas) - corpus.areas:
            raise ValueError(f"{question.id}: неизвестная область {', '.join(sorted(unknown))}")
        for reference in question.expected_clauses:
            _check_reference(reference, question, corpus)
    return questions
