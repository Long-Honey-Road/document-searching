"""HTTP-модели ОКК и сообщения публикации практики.

Сообщения повторяют общий контракт ``regagent_common.contracts`` (D-61), но с типами
backend (UUID), чтобы мусор из очереди отвергался до записи в БД.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from regagent_common.contracts import PRACTICE_BINDS_MAX, PRACTICE_TEXT_MAX, PracticeBinding

TicketStatus = Literal["new", "publishing", "published", "rejected", "publish_failed"]
Mode = Literal["clarifies", "overrides"]
Origin = Literal["employee", "analytics", "recheck"]
RecheckReason = Literal["new_version", "access_changed", "deleted", "content_changed"]
NO_NUL = r"^[^\x00]*$"
Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=NO_NUL)
]
Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000, pattern=NO_NUL)
]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Revision = Annotated[int, Field(ge=1, le=2_147_483_647, strict=True)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CreateTicket(InputModel):
    submission_id: Identifier
    question: Question
    comment: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000, pattern=NO_NUL)]
        | None
    ) = None


class Binding(InputModel):
    doc_id: Identifier
    version: Revision
    clause: Identifier

    @model_validator(mode="after")
    def knowledge_accepts(self) -> Self:
        # Те же правила, что у knowledge: иначе публикация упадёт уже после отправки
        PracticeBinding.model_validate(self.model_dump())
        return self


class PublishDraft(InputModel):
    text: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=PRACTICE_TEXT_MAX, pattern=NO_NUL
        ),
    ]
    mode: Mode
    binds: Annotated[list[Binding], Field(min_length=1, max_length=PRACTICE_BINDS_MAX)]

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        keys = {(item.doc_id, item.version, item.clause) for item in self.binds}
        if len(keys) != len(self.binds):
            raise ValueError("duplicate binding")
        return self


class Transition(BaseModel):
    status: TicketStatus
    actor_id: str
    at: datetime
    error_code: ErrorCode | None = None


class TicketSummary(BaseModel):
    id: UUID
    question: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime


class ReviewSummary(TicketSummary):
    kind: Origin


class RecheckDetails(BaseModel):
    reason: RecheckReason
    doc_id: Identifier
    version: Revision | None


class TicketDetail(ReviewSummary):
    comment: str | None
    draft: PublishDraft | None
    history: list[Transition]
    revision: int
    practice_id: UUID
    recheck: RecheckDetails | None


class Message(InputModel):
    message_id: UUID
    created_at: AwareDatetime


class PracticePublish(Message, PublishDraft):
    message_id: UUID = Field(default_factory=uuid4)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    type: Literal["practice.publish"] = "practice.publish"
    ticket_id: UUID
    practice_id: UUID
    revision: Revision
    approved_by: Identifier
    approved_levels: Annotated[list[Identifier], Field(min_length=1)]


class PracticeResult(Message):
    type: Literal["practice.published"]
    ticket_id: UUID
    practice_id: UUID
    revision: Revision
    status: Literal["published", "failed"]
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def error_on_failure(self) -> Self:
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("error_code is required only for a failure")
        return self


class PracticeRecheck(InputModel):
    """Перепроверка одной опубликованной практики: knowledge шлёт по сообщению на практику."""

    message_id: Identifier  # детерминированный у knowledge: recheck:<practice>:<revision>
    created_at: AwareDatetime
    type: Literal["qc.events"]
    kind: Literal["recheck"]
    ticket_id: UUID
    practice_id: UUID
    revision: Revision
    doc_id: Identifier
    reason: RecheckReason
