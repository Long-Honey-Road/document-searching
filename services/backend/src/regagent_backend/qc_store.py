"""Транзакции заявок, истории и очереди исходящих команд ОКК."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from regagent_backend.events import events
from regagent_backend.identity import UserIdentity
from regagent_backend.qc_models import (
    CreateTicket,
    Origin,
    PracticePublish,
    PracticeRecheck,
    PracticeResult,
    PublishDraft,
    RecheckDetails,
    ReviewSummary,
    TicketDetail,
    TicketStatus,
    TicketSummary,
    Transition,
)

ADMIN_ROLE = "admin"
log = logging.getLogger(__name__)
Connection = AsyncConnection[dict[str, Any]]
Pool = AsyncConnectionPool[Connection]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_tickets (
    id uuid PRIMARY KEY,
    practice_id uuid NOT NULL,
    revision integer NOT NULL DEFAULT 0,
    author_id text NOT NULL,
    scope_levels text[] NOT NULL,
    submission_id text NOT NULL,
    kind text NOT NULL,
    question text NOT NULL,
    comment text,
    draft jsonb,
    recheck jsonb,
    status text NOT NULL DEFAULT 'new',
    history jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS qc_employee_submission
    ON qc_tickets (author_id, submission_id) WHERE kind = 'employee';
CREATE UNIQUE INDEX IF NOT EXISTS qc_system_submission
    ON qc_tickets (kind, submission_id) WHERE kind <> 'employee';
CREATE UNIQUE INDEX IF NOT EXISTS qc_practice_revision
    ON qc_tickets (practice_id, revision) WHERE revision > 0;
CREATE INDEX IF NOT EXISTS qc_ticket_scope ON qc_tickets USING gin (scope_levels);
CREATE INDEX IF NOT EXISTS qc_ticket_author ON qc_tickets (author_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS qc_outbox (
    id uuid PRIMARY KEY,
    body jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qc_outbox_ready ON qc_outbox (next_attempt_at);
CREATE TABLE IF NOT EXISTS qc_analytics (
    run_id uuid PRIMARY KEY,
    author_id text NOT NULL,
    scope_levels text[] NOT NULL,
    occurred_at timestamptz NOT NULL,
    message_id text,
    area text,
    question text NOT NULL,
    normalized text NOT NULL,
    group_id uuid NOT NULL,
    fragment_ids text[] NOT NULL,
    used_practice boolean NOT NULL,
    outcome text NOT NULL,
    reason text
);
CREATE INDEX IF NOT EXISTS qc_analytics_time ON qc_analytics (occurred_at);
CREATE INDEX IF NOT EXISTS qc_analytics_scope ON qc_analytics USING gin (scope_levels);
CREATE TABLE IF NOT EXISTS qc_recheck_events (
    id uuid PRIMARY KEY
);
"""


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Conflict(Exception):
    pass


def review_levels(identity: UserIdentity) -> list[str]:
    if ADMIN_ROLE not in identity.roles or not identity.levels:
        raise Forbidden
    return sorted(identity.levels)


def qc_event(
    event: str,
    status: str,
    *,
    duration_ms: int = 0,
    error_code: str | None = None,
    **ids: UUID,
) -> None:
    # Общий формат событий (D-52): одна JSON-строка в stdout, поля не теряются в тексте лога
    events.emit_fields(
        event,
        {name: str(value) for name, value in ids.items()},
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
    )


class QCStore:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    async def initialize(self) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            # Aegra может запускать несколько копий backend на одной БД.
            await conn.execute("SELECT pg_advisory_xact_lock(17051044)")
            await conn.execute(_SCHEMA)

    async def create(self, identity: UserIdentity, payload: CreateTicket) -> TicketSummary:
        if not identity.levels:
            raise Forbidden
        async with self.pool.connection() as conn, conn.transaction():
            row, created = await self.insert_ticket(
                conn,
                payload,
                author_id=identity.sub,
                scope_levels=sorted(identity.levels),
                actor_id=identity.sub,
            )
            ticket = TicketSummary.model_validate(row)
        if created:
            qc_event("qc.ticket.created", "new", ticket_id=ticket.id)
        return ticket

    async def insert_ticket(
        self,
        conn: Connection,
        payload: CreateTicket,
        *,
        author_id: str,
        scope_levels: list[str],
        actor_id: str,
        kind: Origin = "employee",
        prior: TicketDetail | None = None,
        recheck: RecheckDetails | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC)
        initial = Transition(status="new", actor_id=actor_id, at=now)
        row = await (
            await conn.execute(
                """
            INSERT INTO qc_tickets (
                id, practice_id, author_id, scope_levels, submission_id, kind,
                question, comment, draft, history, created_at, updated_at, recheck
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING RETURNING *
            """,
                (
                    uuid4(),
                    prior.practice_id if prior else uuid4(),
                    author_id,
                    scope_levels,
                    payload.submission_id,
                    kind,
                    payload.question,
                    payload.comment,
                    Jsonb(prior.draft.model_dump(mode="json")) if prior and prior.draft else None,
                    Jsonb([initial.model_dump(mode="json")]),
                    now,
                    now,
                    Jsonb(recheck.model_dump(mode="json")) if recheck else None,
                ),
            )
        ).fetchone()
        if row is not None:
            return row, True
        existing = await (
            await conn.execute(
                """SELECT * FROM qc_tickets WHERE kind = %s AND submission_id = %s
               AND (kind <> 'employee' OR author_id = %s)""",
                (kind, payload.submission_id, author_id),
            )
        ).fetchone()
        if existing is None:
            raise Conflict
        return existing, False

    async def mine(
        self,
        identity: UserIdentity,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TicketSummary]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                SELECT id, question, status, created_at, updated_at FROM qc_tickets
                WHERE author_id = %s AND kind = 'employee'
                ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s
                """,
                    (identity.sub, limit, offset),
                )
            ).fetchall()
        return [TicketSummary.model_validate(row) for row in rows]

    async def review_queue(
        self,
        identity: UserIdentity,
        *,
        status: TicketStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReviewSummary]:
        levels = review_levels(identity)
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                SELECT id, question, status, created_at, updated_at, kind
                FROM qc_tickets WHERE scope_levels <@ %s::text[]
                    AND (%s::text IS NULL OR status = %s)
                ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s
                """,
                    (levels, status, status, limit, offset),
                )
            ).fetchall()
        return [ReviewSummary.model_validate(row) for row in rows]

    async def _visible(
        self,
        conn: Connection,
        ticket_id: UUID,
        levels: list[str],
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        query = "SELECT * FROM qc_tickets WHERE id = %s AND scope_levels <@ %s::text[]"
        if lock:
            query += " FOR UPDATE"
        row = await (await conn.execute(query, (ticket_id, levels))).fetchone()
        if row is None:
            raise NotFound
        return row

    async def review(self, identity: UserIdentity, ticket_id: UUID) -> TicketDetail:
        levels = review_levels(identity)
        async with self.pool.connection() as conn:
            return TicketDetail.model_validate(await self._visible(conn, ticket_id, levels))

    async def _transition(
        self,
        conn: Connection,
        ticket_id: UUID,
        status: TicketStatus,
        actor_id: str,
        *,
        error_code: str | None = None,
    ) -> TicketDetail:
        at = datetime.now(UTC)
        entry = Transition(status=status, actor_id=actor_id, at=at, error_code=error_code)
        row = await (
            await conn.execute(
                """
            UPDATE qc_tickets SET status = %s, history = history || %s, updated_at = %s
            WHERE id = %s RETURNING *
            """,
                (status, Jsonb([entry.model_dump(mode="json")]), at, ticket_id),
            )
        ).fetchone()
        return TicketDetail.model_validate(row)

    async def publish(
        self,
        identity: UserIdentity,
        ticket_id: UUID,
        draft: PublishDraft,
    ) -> TicketDetail:
        levels = review_levels(identity)
        async with self.pool.connection() as conn, conn.transaction():
            row = await self._visible(conn, ticket_id, levels, lock=True)
            ticket = TicketDetail.model_validate(row)
            if (
                ticket.status in {"publishing", "published", "publish_failed"}
                and ticket.draft == draft
            ):
                return ticket
            if ticket.status != "new":
                raise Conflict
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(ticket.practice_id),)
            )
            latest = await (
                await conn.execute(
                    """
                SELECT revision, status FROM qc_tickets
                WHERE practice_id = %s AND revision > 0 ORDER BY revision DESC LIMIT 1
                """,
                    (ticket.practice_id,),
                )
            ).fetchone()
            if latest and latest["status"] == "publishing":
                raise Conflict
            revision = latest["revision"] + 1 if latest else 1
            command = PracticePublish(
                ticket_id=ticket_id,
                practice_id=ticket.practice_id,
                revision=revision,
                approved_by=identity.sub,
                approved_levels=levels,
                **draft.model_dump(),
            )
            await conn.execute(
                "UPDATE qc_tickets SET draft = %s, revision = %s, scope_levels = %s WHERE id = %s",
                (Jsonb(draft.model_dump(mode="json")), revision, levels, ticket_id),
            )
            await conn.execute(
                "INSERT INTO qc_outbox (id, body) VALUES (%s, %s)",
                (command.message_id, Jsonb(command.model_dump(mode="json"))),
            )
            updated = await self._transition(conn, ticket_id, "publishing", identity.sub)
        qc_event(
            "qc.ticket.transition",
            "publishing",
            ticket_id=ticket_id,
            practice_id=updated.practice_id,
        )
        return updated

    async def reject(self, identity: UserIdentity, ticket_id: UUID) -> TicketDetail:
        levels = review_levels(identity)
        async with self.pool.connection() as conn, conn.transaction():
            row = await self._visible(conn, ticket_id, levels, lock=True)
            if row["status"] == "rejected":
                return TicketDetail.model_validate(row)
            if row["status"] != "new":
                raise Conflict
            updated = await self._transition(conn, ticket_id, "rejected", identity.sub)
        qc_event("qc.ticket.transition", "rejected", ticket_id=ticket_id)
        return updated

    async def dispatch_one(self, send: Callable[[PracticePublish], Awaitable[None]]) -> bool:
        started = monotonic()
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    """
                SELECT id, body, attempts FROM qc_outbox WHERE next_attempt_at <= now()
                ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                )
            ).fetchone()
            if row is None:
                return False
            command = PracticePublish.model_validate(row["body"])
            status, error = "sent", None
            try:
                async with asyncio.timeout(5):
                    await send(command)
            except Exception:
                status, error = "retry", "broker_unavailable"
                delay = 2 ** min(row["attempts"], 8)
                await conn.execute(
                    """UPDATE qc_outbox SET attempts = attempts + 1,
                       next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s""",
                    (delay, row["id"]),
                )
            else:
                await conn.execute("DELETE FROM qc_outbox WHERE id = %s", (row["id"],))
        qc_event(
            "qc.publication.delivery",
            status,
            error_code=error,
            duration_ms=int((monotonic() - started) * 1000),
            ticket_id=command.ticket_id,
            practice_id=command.practice_id,
        )
        return True

    async def apply_result(self, result: PracticeResult) -> bool:
        status: TicketStatus = "published" if result.status == "published" else "publish_failed"
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    """
                SELECT updated_at FROM qc_tickets WHERE id = %s AND practice_id = %s
                    AND revision = %s AND status = 'publishing' FOR UPDATE
                """,
                    (result.ticket_id, result.practice_id, result.revision),
                )
            ).fetchone()
            if row is None:
                return False
            updated = await self._transition(
                conn,
                result.ticket_id,
                status,
                "knowledge",
                error_code=result.error_code,
            )
        elapsed = updated.updated_at - row["updated_at"]
        qc_event(
            "qc.publication.result",
            status,
            error_code=result.error_code,
            duration_ms=max(0, int(elapsed.total_seconds() * 1000)),
            ticket_id=result.ticket_id,
            practice_id=result.practice_id,
        )
        return True

    async def apply_recheck(self, event: PracticeRecheck) -> int:
        """Открывает заявку на перепроверку практики; повтор того же сигнала — без новой заявки."""
        async with self.pool.connection() as conn, conn.transaction():
            recorded = await conn.execute(
                "INSERT INTO qc_recheck_events (id) VALUES (%s) ON CONFLICT DO NOTHING",
                (uuid_for_key(f"recheck:{event.message_id}"),),
            )
            if not recorded.rowcount:
                return 0
            row = await (
                await conn.execute(
                    """
                SELECT * FROM qc_tickets
                WHERE id = %s AND practice_id = %s AND revision = %s AND status = 'published'
                """,
                    (event.ticket_id, event.practice_id, event.revision),
                )
            ).fetchone()
            if row is None:
                return 0
            original = TicketDetail.model_validate(row)
            if original.draft is None or all(
                item.doc_id != event.doc_id for item in original.draft.binds
            ):
                return 0
            key = f"{original.id}:{event.reason}:{event.doc_id}:{event.revision}"
            new_row, created = await self.insert_ticket(
                conn,
                CreateTicket(
                    submission_id=str(uuid_for_key(key)),
                    question=original.question,
                    comment=original.comment,
                ),
                author_id=row["author_id"],
                scope_levels=row["scope_levels"],
                actor_id="knowledge",
                kind="recheck",
                prior=original,
                recheck=RecheckDetails(doc_id=event.doc_id, reason=event.reason, version=None),
            )
        if not created:
            return 0
        ticket = TicketDetail.model_validate(new_row)
        qc_event("qc.ticket.recheck", "new", ticket_id=ticket.id, practice_id=ticket.practice_id)
        return 1


def uuid_for_key(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, key)
