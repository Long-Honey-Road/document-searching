"""Сообщения публикации практики: backend (B) и knowledge (A) читают друг друга (D-61)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from regagent_backend import qc_models
from regagent_common import contracts

BIND = {"doc_id": "dib-rp", "version": 2, "clause": "2.7"}


def draft(**extra: object) -> dict[str, object]:
    return {"text": "Ответ ОКК", "mode": "overrides", "binds": [BIND], **extra}


def backend_command() -> qc_models.PracticePublish:
    return qc_models.PracticePublish(
        ticket_id=uuid4(),
        practice_id=uuid4(),
        revision=1,
        approved_by="admin",
        approved_levels=["public"],
        **qc_models.PublishDraft.model_validate(draft()).model_dump(),
    )


def test_knowledge_accepts_backend_publish_command() -> None:
    command = backend_command()
    seen = contracts.PracticePublish.model_validate_json(command.model_dump_json())
    assert (seen.approved_by, seen.approved_levels) == ("admin", ["public"])
    assert (seen.practice_id, seen.ticket_id) == (str(command.practice_id), str(command.ticket_id))
    assert seen.binds == [contracts.PracticeBinding.model_validate(BIND)]


@pytest.mark.parametrize(
    "bad",
    [
        {"binds": [{**BIND, "clause": str(n)} for n in range(1, 18)]},
        {"binds": [{**BIND, "doc_id": "Правила ДИБ"}]},
        {"binds": [{**BIND, "clause": "2 7"}]},
        {"text": "x" * 12001},
    ],
)
def test_backend_rejects_drafts_knowledge_would_reject(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        qc_models.PublishDraft.model_validate(draft(**bad))
    with pytest.raises(ValidationError):
        contracts.PracticePublish.model_validate(
            {
                "ticket_id": "t",
                "practice_id": "p",
                "revision": 1,
                "approved_by": "admin",
                "approved_levels": ["public"],
                **draft(**bad),
            }
        )


@pytest.mark.parametrize("status", ["published", "failed"])
def test_backend_accepts_knowledge_publication_result(status: str) -> None:
    command = backend_command()
    result = contracts.PracticePublicationResult(
        ticket_id=str(command.ticket_id),
        practice_id=str(command.practice_id),
        revision=1,
        status=status,  # type: ignore[arg-type]
        error_code="index_failed" if status == "failed" else None,
    )
    seen = qc_models.PracticeResult.model_validate_json(result.model_dump_json())
    assert (seen.ticket_id, seen.status) == (command.ticket_id, status)


@pytest.mark.parametrize("reason", ["new_version", "deleted", "access_changed", "content_changed"])
def test_backend_accepts_knowledge_recheck(reason: str) -> None:
    command = backend_command()
    event = contracts.PracticeRecheck(
        message_id=f"recheck:{command.practice_id}:1",
        ticket_id=str(command.ticket_id),
        practice_id=str(command.practice_id),
        revision=1,
        doc_id="dib-rp",
        reason=reason,  # type: ignore[arg-type]
    )
    seen = qc_models.PracticeRecheck.model_validate_json(event.model_dump_json())
    assert (seen.ticket_id, seen.practice_id, seen.revision) == (
        command.ticket_id,
        command.practice_id,
        1,
    )
    assert (seen.doc_id, seen.reason, seen.message_id) == ("dib-rp", reason, event.message_id)
