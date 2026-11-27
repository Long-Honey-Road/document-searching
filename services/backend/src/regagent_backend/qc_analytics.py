"""Внутренний стык графа агента и админская аналитика ОКК."""

from __future__ import annotations

import asyncio
import json
import unicodedata
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from aegra_api.core.database import db_manager
from pydantic import AwareDatetime, BaseModel, Field

from regagent_backend.identity import UserIdentity
from regagent_backend.qc_models import (
    CreateTicket,
    ErrorCode,
    Identifier,
    InputModel,
    Question,
    TicketStatus,
    TicketSummary,
)
from regagent_backend.qc_store import NotFound, QCStore, qc_event, review_levels, uuid_for_key
from regagent_backend.settings import BackendSettings

Outcome = Literal[
    "answer",
    "empty_search",
    "insufficient_evidence",
    "clarification",
    "access_limited",
    "search_unavailable",
    "model_timeout",
    "refusal",
    "error",  # сбой хода, не таймаут модели (D-54)
]
UNANSWERED: tuple[Outcome, ...] = (
    "empty_search",
    "insufficient_evidence",
    "clarification",
    "access_limited",
    "search_unavailable",
    "model_timeout",
    "error",
)


class TurnOutcome(InputModel):
    run_id: UUID
    occurred_at: AwareDatetime
    question: Question
    message_id: Identifier | None = None
    area: Identifier | None = None
    fragment_ids: Annotated[list[Identifier], Field(max_length=500)] = Field(default_factory=list)
    used_practice: bool = False
    outcome: Outcome
    reason: ErrorCode | None = None


class AnalyticsFilters(InputModel):
    days: Annotated[int, Field(ge=1, le=365)] = 30
    area: Identifier | None = None
    outcome: Outcome | None = None
    ticket_status: TicketStatus | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0, le=2_147_483_647)] = 0


class QuestionGroup(BaseModel):
    run_id: UUID
    occurred_at: datetime
    question: str
    area: str | None
    count: int
    outcome: Outcome
    reason: ErrorCode | None
    fragment_ids: list[str]
    used_practice: bool
    ticket_status: TicketStatus | None


class AnalyticsReport(BaseModel):
    total: int
    used_practice: int
    by_outcome: dict[Outcome, int]
    groups: list[QuestionGroup]


def normalize_question(question: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", question).casefold().split())
    return normalized.rstrip("?.! ") or normalized


_REPORT_SQL = """
WITH visible AS (
    SELECT run.*, linked.status AS ticket_status
    FROM qc_analytics AS run
    LEFT JOIN LATERAL (
        SELECT ticket.status FROM qc_tickets AS ticket
        WHERE ticket.scope_levels <@ %(levels)s::text[]
          AND (
            (ticket.kind = 'employee' AND ticket.author_id = run.author_id
             AND ticket.submission_id = run.message_id)
            OR
            (ticket.kind = 'analytics' AND ticket.submission_id = run.group_id::text)
          )
        ORDER BY ticket.updated_at DESC, ticket.id DESC LIMIT 1
    ) AS linked ON true
    WHERE run.scope_levels <@ %(levels)s::text[] AND run.occurred_at >= %(since)s
          AND (%(area)s::text IS NULL OR run.area = %(area)s)
          AND (%(outcome)s::text IS NULL OR run.outcome = %(outcome)s)
          AND (%(ticket_status)s::text IS NULL OR linked.status = %(ticket_status)s)
),
ranked AS (
    SELECT run_id, occurred_at, question, area, outcome, reason, fragment_ids, used_practice,
           ticket_status, COUNT(*) OVER group_rows AS count,
           ROW_NUMBER() OVER (group_rows ORDER BY occurred_at DESC, run_id DESC) AS rank
    FROM visible WHERE outcome = ANY(%(unanswered)s)
    WINDOW group_rows AS (PARTITION BY area, normalized)
),
page AS (
    SELECT * FROM ranked WHERE rank = 1
    ORDER BY count DESC, occurred_at DESC, run_id DESC LIMIT %(limit)s OFFSET %(offset)s
)
SELECT
    (SELECT COUNT(*) FROM visible) AS total,
    (SELECT COUNT(*) FROM visible WHERE used_practice) AS used_practice,
    COALESCE((SELECT jsonb_object_agg(outcome, n)
        FROM (SELECT outcome, COUNT(*) AS n FROM visible GROUP BY outcome) AS counts),
        '{}'::jsonb) AS by_outcome,
    COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY count DESC, occurred_at DESC, run_id DESC)
        FROM page), '[]'::jsonb) AS groups
"""


class Analytics:
    def __init__(self, store: QCStore, *, retention_days: int) -> None:
        self.store = store
        self.retention_days = retention_days

    def _cutoff(self, days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=min(days, self.retention_days))

    async def record(self, turn: TurnOutcome, identity: UserIdentity) -> bool:
        if not identity.levels:
            return False
        levels = sorted(identity.levels)
        normalized = normalize_question(turn.question)
        group_id = uuid_for_key("qc.analytics:" + json.dumps([levels, turn.area, normalized]))
        async with self.store.pool.connection() as conn, conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO qc_analytics (
                    run_id, author_id, scope_levels, occurred_at, message_id, area, question,
                    normalized, group_id, fragment_ids, used_practice, outcome, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    turn.run_id,
                    identity.sub,
                    levels,
                    turn.occurred_at,
                    turn.message_id,
                    turn.area,
                    turn.question,
                    normalized,
                    group_id,
                    list(dict.fromkeys(turn.fragment_ids)),
                    turn.used_practice,
                    turn.outcome,
                    turn.reason,
                ),
            )
        return bool(result.rowcount)

    async def report(self, identity: UserIdentity, filters: AnalyticsFilters) -> AnalyticsReport:
        params = {
            **filters.model_dump(),
            "levels": review_levels(identity),
            "since": self._cutoff(filters.days),
            "unanswered": list(UNANSWERED),
        }
        async with self.store.pool.connection() as conn:
            row = await (await conn.execute(_REPORT_SQL, params)).fetchone()
        return AnalyticsReport.model_validate(row)

    async def create_ticket(self, identity: UserIdentity, run_id: UUID) -> TicketSummary:
        levels = review_levels(identity)
        async with self.store.pool.connection() as conn, conn.transaction():
            run = await (
                await conn.execute(
                    """
                SELECT * FROM qc_analytics WHERE run_id = %s AND scope_levels <@ %s::text[]
                    AND occurred_at >= %s AND outcome = ANY(%s)
                """,
                    (run_id, levels, self._cutoff(self.retention_days), list(UNANSWERED)),
                )
            ).fetchone()
            if run is None:
                raise NotFound
            payload = CreateTicket(submission_id=str(run["group_id"]), question=run["question"])
            row, created = await self.store.insert_ticket(
                conn,
                payload,
                author_id=run["author_id"],
                scope_levels=run["scope_levels"],
                actor_id=identity.sub,
                kind="analytics",
            )
            result = TicketSummary.model_validate(row)
        if created:
            qc_event("qc.ticket.created", "new", ticket_id=result.id, run_id=run_id)
        return result

    async def prune(self) -> int:
        async with self.store.pool.connection() as conn, conn.transaction():
            deleted = await conn.execute(
                "DELETE FROM qc_analytics WHERE occurred_at < %s",
                (self._cutoff(self.retention_days),),
            )
        return deleted.rowcount


async def record_turn(turn: TurnOutcome, identity: UserIdentity) -> bool:
    """Записывает итог без влияния на ответ при недоступной БД."""
    started = monotonic()
    try:
        settings = BackendSettings()
        pool = db_manager.lg_pool
        if pool is None:
            raise RuntimeError("application database unavailable")
        async with asyncio.timeout(2):
            recorded = await Analytics(
                QCStore(pool), retention_days=settings.AUDIT_RETENTION_DAYS
            ).record(turn, identity)
    except Exception:
        qc_event(
            "qc.analytics.write",
            "failed",
            error_code="analytics_unavailable",
            duration_ms=int((monotonic() - started) * 1000),
            run_id=turn.run_id,
        )
        return False
    qc_event(
        "qc.analytics.write",
        "recorded" if recorded else "skipped",
        duration_ms=int((monotonic() - started) * 1000),
        run_id=turn.run_id,
    )
    return recorded
