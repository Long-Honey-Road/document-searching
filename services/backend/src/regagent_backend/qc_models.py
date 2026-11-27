"""HTTP-модели ОКК и адаптер протокола публикации для потока A."""

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

TicketStatus = Literal["new", "publishing", "published", "rejected", "publish_failed"]
Mode = Literal["clarifies", "overrides"]
Origin = Literal["employee", "analytics", "recheck"]
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


class PublishDraft(InputModel):
    text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=12000, pattern=NO_NUL),
    ]
    mode: Mode
    binds: Annotated[list[Binding], Field(min_length=1, max_length=20)]

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
    reason: Literal["new_version", "access_changed", "deleted"]
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
    admin_id: Identifier
    admin_levels: list[Identifier]


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


class PracticeRecheck(Message):
    type: Literal["qc.events"]
    kind: Literal["recheck"]
    reason: Literal["new_version", "access_changed", "deleted"]
    doc_id: Identifier
    version: Revision | None
    practice_ids: Annotated[list[UUID], Field(max_length=200)]

    @model_validator(mode="after")
    def require_version(self) -> Self:
        if self.reason != "deleted" and self.version is None:
            raise ValueError("document version is required")
        return self
