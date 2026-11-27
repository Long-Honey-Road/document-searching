"""Адаптер итога хода (C) к аналитике ОКК (B)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from regagent_backend import qc_analytics
from regagent_backend.identity import UserIdentity
from regagent_backend.qc_turn_sink import QcTurnSink
from regagent_common.contracts import TurnReport

RUN = "6f1c2e9a-0000-4000-8000-000000000001"
IDENTITY = UserIdentity(
    sub="reader", username="reader", levels=frozenset({"public"}), groups=(), roles=()
)


def report(outcome: str, **extra: Any) -> TurnReport:
    return TurnReport.model_validate(
        {
            "run_id": RUN,
            "thread_id": "t",
            "user_sub": "reader",
            "at": datetime(2026, 11, 28, 2, tzinfo=UTC),
            "question": "Кто назначает РП?",
            "area": "ДИБ",
            "area_source": "question",
            "outcome": outcome,
            "reason": None,
            "fragments": ["dib@1#2.7", "practice:p1"],
            "cited": ["practice:p1"],
            "practice_used": True,
            "searches": 1,
            **extra,
        }
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, UserIdentity]]:
    calls: list[tuple[Any, UserIdentity]] = []

    async def record_turn(turn: Any, identity: UserIdentity) -> bool:
        calls.append((turn, identity))
        return True

    monkeypatch.setattr(qc_analytics, "record_turn", record_turn)
    return calls


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("answered", "answer"),
        ("empty_search", "empty_search"),
        ("unsupported", "insufficient_evidence"),
        ("ask_area", "clarification"),
        ("refused", "refusal"),
        ("search_unavailable", "search_unavailable"),
        ("model_timeout", "model_timeout"),
    ],
)
async def test_outcomes_map_to_analytics(
    recorded: list[tuple[Any, UserIdentity]], ours: str, theirs: str
) -> None:
    await QcTurnSink().record(report(ours, reason="low_confidence"), IDENTITY)
    [(turn, identity)] = recorded
    assert turn.outcome == theirs and identity is IDENTITY
    assert turn.run_id == UUID(RUN) and turn.question == "Кто назначает РП?"
    assert (turn.area, turn.fragment_ids, turn.used_practice) == (
        "ДИБ",
        ["dib@1#2.7", "practice:p1"],
        True,
    )
    assert turn.reason == "low_confidence"


async def test_generic_error_is_not_recorded_until_analytics_supports_it(
    recorded: list[tuple[Any, UserIdentity]],
) -> None:
    with pytest.raises(ValueError, match="error"):
        await QcTurnSink().record(report("error"), IDENTITY)
    assert not recorded
