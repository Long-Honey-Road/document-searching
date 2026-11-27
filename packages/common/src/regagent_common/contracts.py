"""Модели контрактов из dev_docs/06-contracts.md."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

FragmentKind = Literal["regulation", "practice"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- knowledge API ---


class Fragment(_Model):
    fragment_id: str
    doc_id: str
    version: int | None  # None — у фрагмента практики (TECH-DEBT TD-01)
    title: str
    clause: str
    area: str
    kind: FragmentKind
    text: str
    score: float


class SearchRequest(_Model):
    query: str = Field(min_length=1)
    levels: list[str]
    areas: list[str] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1)  # None — SEARCH_TOP_K из настроек knowledge


class SearchResponse(_Model):
    fragments: list[Fragment]
    areas_found: list[str]
    areas_diverge: bool
    low_confidence: bool


class ClauseRequest(_Model):
    doc_id: str
    clause: str
    version: int | None = None  # None — действующая редакция
    levels: list[str]


class ClauseResponse(_Model):
    fragment: Fragment
    neighbors: list[Fragment]


class Area(_Model):
    """Запись справочника областей (секция ``areas:`` манифеста)."""

    code: str
    title: str
    synonyms: list[str] = Field(default_factory=list)


class AreasResponse(_Model):
    """``GET /areas`` knowledge — справочник областей для backend (проверка памяти)."""

    areas: list[Area]


# --- RabbitMQ ---


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class IndexJob(_Model):
    """Задача ``index.jobs``. Обработка идемпотентна по ``(doc_id, version, sha256)``."""

    message_id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    type: Literal["index.jobs"] = "index.jobs"
    job_id: str = Field(default_factory=_new_id)
    op: Literal["upsert", "delete"]
    doc_id: str
    version: int
    path: str | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def _upsert_needs_file(self) -> Self:
        if self.op == "upsert" and (not self.path or not self.sha256):
            raise ValueError("для upsert нужны path и sha256")
        return self


# --- ответ агента ---


class AnswerSource(_Model):
    n: int
    fragment_id: str
    title: str
    clause: str
    version: int | None
    kind: FragmentKind


class AnswerMeta(_Model):
    verified: bool
    area_used: str | None
    area_source: Literal["memory", "question", "asked", "none"]
    sources: list[AnswerSource]
    outcome: Literal["answer", "refusal", "not_found", "fallback"]
