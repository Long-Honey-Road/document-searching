import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aio_pika
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from regagent_backend.identity import UserIdentity
from regagent_backend.qc_analytics import Analytics, AnalyticsFilters, TurnOutcome
from regagent_backend.qc_models import (
    CreateTicket,
    PracticePublish,
    PracticeRecheck,
    PracticeResult,
    PublishDraft,
)
from regagent_backend.qc_store import Conflict, Connection, NotFound, QCStore
from regagent_backend.qc_worker import COMMAND_QUEUE, EXCHANGE, RESULT_KEY, broker_session

pytestmark = pytest.mark.integration

EMPLOYEE = UserIdentity("employee", "employee", frozenset(), frozenset(), frozenset({"public"}))
CLOSED = UserIdentity("closed", "closed", frozenset(), frozenset(), frozenset({"public", "fin"}))
ADMIN = UserIdentity("admin", "admin", frozenset(), frozenset({"admin"}), EMPLOYEE.levels)


@pytest.fixture
async def store() -> AsyncIterator[QCStore]:
    settings = {
        name.lower(): os.environ.get(f"TEST_POSTGRES_{suffix}", "")
        for name, suffix in [
            ("host", "HOST"),
            ("port", "PORT"),
            ("user", "USER"),
            ("password", "PASSWORD"),
            ("dbname", "DB"),
        ]
    }
    if not settings["host"]:
        pytest.fail("для integration-теста нужен отдельный TEST_POSTGRES_HOST")
    schema = f"qc_test_{uuid4().hex}"
    async with (
        AsyncConnectionPool(
            kwargs={**settings, "row_factory": dict_row}, open=False, min_size=1, max_size=4
        ) as pool,
        pool.connection() as conn,
    ):
        # UUID гарантирует корректный идентификатор тестовой схемы.
        await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        async with AsyncConnectionPool[Connection](
            kwargs={**settings, "options": f"-c search_path={schema}", "row_factory": dict_row},
            open=False,
            min_size=1,
            max_size=4,
        ) as app_pool:
            qc = QCStore(app_pool)
            await qc.initialize()
            yield qc
    finally:
        async with (
            AsyncConnectionPool(kwargs=settings, open=False, min_size=1, max_size=1) as cleanup,
            cleanup.connection() as conn,
        ):
            await conn.execute(f"DROP SCHEMA {schema} CASCADE")


def draft() -> PublishDraft:
    return PublishDraft.model_validate(
        {
            "text": "Ответ ОКК",
            "mode": "clarifies",
            "binds": [{"doc_id": "rule", "version": 2, "clause": "1.1"}],
        }
    )


async def test_retry_result_and_recheck(store: QCStore) -> None:
    submission = CreateTicket(submission_id="msg-1", question="Как оформить заявку?")
    ticket = await store.create(EMPLOYEE, submission)
    assert (await store.create(EMPLOYEE, submission)).id == ticket.id
    assert (await store.mine(EMPLOYEE))[0].status == "new"
    assert not await store.mine(CLOSED)

    closed = await store.create(CLOSED, submission)
    with pytest.raises(NotFound):
        await store.publish(ADMIN, closed.id, draft())
    assert [item.id for item in await store.review_queue(ADMIN)] == [ticket.id]
    review = await store.publish(ADMIN, ticket.id, draft())
    assert review.status == "publishing"
    assert review.history[-1].actor_id == ADMIN.sub
    with pytest.raises(Conflict):
        await store.reject(ADMIN, ticket.id)
    assert (await store.publish(ADMIN, ticket.id, draft())).status == "publishing"

    sent: list[PracticePublish] = []

    async def broker(command: PracticePublish) -> None:
        sent.append(command)
        if len(sent) == 1:
            raise OSError("broker unavailable after acceptance")

    assert await store.dispatch_one(broker)
    assert (await store.review(ADMIN, ticket.id)).status == "publishing"
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE qc_outbox SET next_attempt_at = now()")
    assert await store.dispatch_one(broker)
    assert not await store.dispatch_one(broker)
    assert sent[0] == sent[1]
    command = sent[0]
    assert command.admin_id == ADMIN.sub and command.admin_levels == ["public"]

    result = PracticeResult.model_validate(
        {
            "type": "practice.published",
            "message_id": uuid4(),
            "created_at": datetime.now(UTC),
            "ticket_id": ticket.id,
            "practice_id": command.practice_id,
            "revision": command.revision,
            "status": "published",
        }
    )
    assert not await store.apply_result(result.model_copy(update={"revision": 2}))
    assert await store.apply_result(result)
    assert not await store.apply_result(result.model_copy(update={"message_id": uuid4()}))
    assert (await store.review(ADMIN, ticket.id)).status == "published"

    event = PracticeRecheck.model_validate(
        {
            "type": "qc.events",
            "message_id": uuid4(),
            "created_at": datetime.now(UTC),
            "kind": "recheck",
            "reason": "new_version",
            "doc_id": "rule",
            "version": 3,
            "practice_ids": [command.practice_id],
        }
    )
    assert await store.apply_recheck(event) == 1
    assert await store.apply_recheck(event) == 0
    recheck = (await store.review_queue(ADMIN, status="new"))[0]
    assert recheck.kind == "recheck"
    assert (await store.publish(ADMIN, recheck.id, draft())).revision == 2
    assert await store.apply_result(
        result.model_copy(
            update={
                "ticket_id": recheck.id,
                "revision": 2,
                "status": "failed",
                "error_code": "index_failed",
            }
        )
    )
    assert (await store.review(ADMIN, recheck.id)).status == "publish_failed"


async def test_analytics_filters_groups_scopes_and_retention(store: QCStore) -> None:
    analytics = Analytics(store, retention_days=2)
    now = datetime.now(UTC)
    first = TurnOutcome(
        run_id=uuid4(),
        message_id="first",
        occurred_at=now - timedelta(minutes=10),
        area="ДИБ",
        question=" Кто   согласует? ",
        outcome="empty_search",
    )
    second = first.model_copy(
        update={
            "run_id": uuid4(),
            "message_id": "second",
            "occurred_at": now,
            "question": "кто согласует",
            "outcome": "insufficient_evidence",
            "fragment_ids": ["rule@2#1.1"],
        }
    )
    restricted = second.model_copy(update={"run_id": uuid4(), "question": "Закрытый вопрос"})
    expired = second.model_copy(
        update={
            "run_id": uuid4(),
            "occurred_at": now - timedelta(days=3),
            "question": "Старый вопрос",
        }
    )
    for identity, turn in [
        (EMPLOYEE, first),
        (EMPLOYEE, second),
        (CLOSED, restricted),
        (EMPLOYEE, expired),
    ]:
        assert await analytics.record(turn, identity)
    assert not await analytics.record(first, EMPLOYEE)

    report = await analytics.report(ADMIN, AnalyticsFilters(area="ДИБ"))
    assert report.total == 2
    (group,) = report.groups
    assert group.count == 2 and group.run_id == second.run_id
    assert report.groups[0].fragment_ids == ["rule@2#1.1"]
    with pytest.raises(NotFound):
        await analytics.create_ticket(ADMIN, restricted.run_id)
    with pytest.raises(NotFound):
        await analytics.create_ticket(ADMIN, expired.run_id)

    await store.create(EMPLOYEE, CreateTicket(submission_id="first", question=first.question))
    filtered = await analytics.report(ADMIN, AnalyticsFilters(ticket_status="new"))
    assert filtered.total == 1
    assert filtered.groups[0].run_id == first.run_id
    existing = await analytics.create_ticket(ADMIN, second.run_id)
    assert (await analytics.create_ticket(ADMIN, second.run_id)).id == existing.id
    assert await analytics.prune() == 1


async def test_double_submit_and_draft_visibility(store: QCStore) -> None:
    submission = CreateTicket(submission_id="msg-1", question="Открытый вопрос")
    first, duplicate = await asyncio.gather(
        store.create(EMPLOYEE, submission), store.create(EMPLOYEE, submission)
    )
    assert first.id == duplicate.id

    restricted_admin = UserIdentity(
        "finance-admin", "finance-admin", frozenset(), ADMIN.roles, CLOSED.levels
    )
    await store.publish(restricted_admin, first.id, draft())
    with pytest.raises(NotFound):
        await store.review(ADMIN, first.id)
    assert not await store.review_queue(ADMIN)
    assert (await store.mine(EMPLOYEE))[0].status == "publishing"
    assert (await store.review(restricted_admin, first.id)).draft == draft()


async def test_rabbitmq_command_and_result_roundtrip(store: QCStore) -> None:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.fail("для integration-теста нужен TEST_RABBITMQ_URL отдельного брокера")
    ticket = await store.create(EMPLOYEE, CreateTicket(submission_id="rabbit", question="Вопрос"))
    await store.publish(ADMIN, ticket.id, draft())

    async with await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        commands = await channel.declare_queue(COMMAND_QUEUE, durable=True)
        worker = asyncio.create_task(broker_session(store, url))
        try:
            async with asyncio.timeout(12), commands.iterator() as messages:
                received = await anext(messages)
                async with received.process():
                    command = PracticePublish.model_validate_json(received.body)
                    assert command.ticket_id == ticket.id
                    assert command.admin_id == ADMIN.sub and command.revision == 1
                result = PracticeResult(
                    message_id=uuid4(),
                    created_at=datetime.now(UTC),
                    type="practice.published",
                    ticket_id=ticket.id,
                    practice_id=command.practice_id,
                    revision=command.revision,
                    status="published",
                )
                for _ in range(2):
                    await exchange.publish(
                        aio_pika.Message(result.model_dump_json().encode()),
                        routing_key=RESULT_KEY,
                    )
                while (  # noqa: ASYNC110 — ждём применения результата RabbitMQ в PostgreSQL
                    reviewed := await store.review(ADMIN, ticket.id)
                ).status == "publishing":
                    await asyncio.sleep(0.025)
                assert [entry.status for entry in reviewed.history] == [
                    "new",
                    "publishing",
                    "published",
                ]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
