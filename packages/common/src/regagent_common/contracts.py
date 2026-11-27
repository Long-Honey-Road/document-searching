"""Модели контрактов из dev_docs/06-contracts.md."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fragments import Mode, is_header_value

FragmentKind = Literal["regulation", "practice"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PracticeBinding(_Model):
    doc_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: int = Field(ge=1)
    clause: str = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_clause(self) -> Self:
        if not is_header_value(self.clause):
            raise ValueError("некорректный номер пункта")
        return self


class PracticeSource(_Model):
    mode: Mode
    binds: list[PracticeBinding] = Field(min_length=1)


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
    practice: PracticeSource | None = Field(default=None, exclude_if=lambda value: value is None)


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


class RefsRequest(_Model):
    doc_id: str
    levels: list[str]


class DocumentRef(_Model):
    """Ссылка между документами из OKF (`links`): ``clause`` — пункт, где упомянут документ,
    ``via`` — упоминание как в тексте."""

    doc_id: str
    title: str
    clause: str
    via: str


class RefsResponse(_Model):
    outgoing: list[DocumentRef]
    incoming: list[DocumentRef]


# --- служебный API knowledge (/admin, P2-B3) ---


class AdminDocument(_Model):
    doc_id: str
    version: int | None
    level: str | None
    areas: list[str]
    title: str | None
    status: str
    error: str | None
    updated_at: datetime | None


class AdminDocuments(_Model):
    documents: list[AdminDocument]


class AdminJob(_Model):
    job_id: str
    op: Literal["upsert", "delete"]
    doc_id: str
    version: int
    status: str
    attempts: int
    error: str | None
    current_step: str | None
    steps: dict[str, float]
    created_at: datetime
    updated_at: datetime


class AdminJobs(_Model):
    jobs: list[AdminJob]


class ReviewItem(_Model):
    """Ссылка по названию, которую не удалось сопоставить: ``ambiguous`` — подходит к
    нескольким документам (``candidates``), ``missing`` — документа нет в корпусе."""

    doc_id: str
    version: int
    title: str
    clause: str
    via: str
    kind: Literal["ambiguous", "missing"]
    candidates: list[str] = Field(default_factory=list)


class AdminReview(_Model):
    items: list[ReviewItem]


class RetryResponse(_Model):
    queued: int


class Area(_Model):
    """Запись справочника областей (секция ``areas:`` манифеста)."""

    code: str
    title: str
    synonyms: list[str] = Field(default_factory=list)


# --- RabbitMQ ---


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class PracticePublish(_Model):
    message_id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    type: Literal["practice.publish"] = "practice.publish"
    ticket_id: str = Field(min_length=1)
    practice_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    revision: int = Field(ge=1)
    text: str = Field(min_length=1)
    binds: list[PracticeBinding] = Field(min_length=1, max_length=16)
    mode: Mode
    approved_by: str = Field(min_length=1)
    approved_levels: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_content(self) -> Self:
        if not self.text.strip() or "\x00" in self.text:
            raise ValueError("пояснение пустое или содержит NUL")
        if len({(b.doc_id, b.version, b.clause) for b in self.binds}) != len(self.binds):
            raise ValueError("повторяющиеся привязки")
        return self


class PracticePublicationResult(_Model):
    message_id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    type: Literal["practice.published"] = "practice.published"
    ticket_id: str
    practice_id: str
    revision: int
    status: Literal["published", "failed"]
    error_code: str | None = None


class PracticeRecheck(_Model):
    message_id: str
    created_at: datetime = Field(default_factory=_now)
    type: Literal["qc.events"] = "qc.events"
    kind: Literal["recheck"] = "recheck"
    ticket_id: str
    practice_id: str
    revision: int
    doc_id: str
    reason: Literal["new_version", "deleted", "access_changed", "content_changed"]


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
    # Переиндексация при том же входе (D-45): не пропускать уже проиндексированный документ
    force: bool = False

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
    outcome: Literal["answer", "refusal", "not_found", "ask_area", "fallback"]
