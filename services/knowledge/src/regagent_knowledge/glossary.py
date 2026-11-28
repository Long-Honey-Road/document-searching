"""Общий словарь сокращений для поиска по регламентам."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .references import lemmas


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abbr: str = Field(pattern=r"^[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]+$")
    full: str = Field(min_length=2)
    case_sensitive: bool = False


@dataclass(frozen=True)
class Alias:
    abbr: str
    full: str
    abbr_pattern: re.Pattern[str]
    full_terms: tuple[str, ...]


@cache
def _aliases() -> tuple[Alias, ...]:
    file = resources.files(__package__).joinpath("glossary.yaml")
    entries = TypeAdapter(list[GlossaryEntry]).validate_python(
        yaml.safe_load(file.read_text(encoding="utf-8"))
    )
    if len({entry.abbr for entry in entries}) != len(entries):
        raise ValueError("В глоссарии есть повторяющиеся сокращения")
    return tuple(
        Alias(
            entry.abbr,
            entry.full,
            re.compile(rf"\b{entry.abbr}\b", 0 if entry.case_sensitive else re.IGNORECASE),
            lemmas(entry.full),
        )
        for entry in entries
    )


def expand_query(query: str) -> str:
    """Добавляет отсутствующую форму термина, сохраняя исходный запрос."""
    terms = lemmas(query)
    additions = []
    for alias in _aliases():
        has_abbr = bool(alias.abbr_pattern.search(query))
        size = len(alias.full_terms)
        has_full = any(
            terms[i : i + size] == alias.full_terms for i in range(len(terms) - size + 1)
        )
        if has_abbr and not has_full:
            additions.append(f"{alias.abbr} — {alias.full}")
        elif has_full and not has_abbr:
            additions.append(alias.abbr)
    return query + " (" + "; ".join(dict.fromkeys(additions)) + ")" if additions else query
