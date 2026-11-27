"""Итог хода агента (D-54) → аналитика ОКК (поток B)."""

from __future__ import annotations

from regagent_common.contracts import TurnReport

from regagent_backend import qc_analytics
from regagent_backend.identity import UserIdentity

# Имена итогов в аналитике B отличаются от TurnReport.outcome
_OUTCOMES: dict[str, qc_analytics.Outcome] = {
    "answered": "answer",
    "empty_search": "empty_search",
    "unsupported": "insufficient_evidence",
    "ask_area": "clarification",
    "refused": "refusal",
    "search_unavailable": "search_unavailable",
    "model_timeout": "model_timeout",
}
_QUESTION_MAX = 4000
_FRAGMENTS_MAX = 500


class QcTurnSink:
    async def record(self, report: TurnReport, identity: UserIdentity) -> None:
        outcome = _OUTCOMES.get(report.outcome)
        if outcome is None:
            # Общей ошибки в аналитике B нет: такой ход остаётся только в событиях
            raise ValueError(f"outcome {report.outcome} is not supported by QC analytics")
        turn = qc_analytics.TurnOutcome(
            run_id=report.run_id,
            occurred_at=report.at,
            question=report.question[:_QUESTION_MAX],
            area=report.area,
            fragment_ids=report.fragments[:_FRAGMENTS_MAX],
            used_practice=report.practice_used,
            outcome=outcome,
            reason=report.reason,
        )
        await qc_analytics.record_turn(turn, identity)
