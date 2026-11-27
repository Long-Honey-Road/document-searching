"""Доставка outbox и приём сообщений Knowledge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aio_pika
from aegra_api.core.database import db_manager
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage, AbstractQueue
from fastapi import FastAPI
from pydantic import ValidationError
from regagent_common import broker

from regagent_backend.qc_analytics import Analytics
from regagent_backend.qc_models import PracticePublish, PracticeRecheck, PracticeResult
from regagent_backend.qc_store import QCStore, qc_event
from regagent_backend.settings import BackendSettings

EXCHANGE = broker.EXCHANGE
COMMAND_QUEUE = broker.PRACTICE_PUBLISH
EVENT_QUEUE = broker.QC_EVENTS
RESULT_KEY = broker.PRACTICE_PUBLISHED


async def declare_topology(channel: Any, exchange: Any) -> dict[str, Any]:
    """Общие очереди с knowledge (D-61); своих у backend нет."""
    return await broker.declare_shared(channel, exchange)


async def _send(exchange: AbstractExchange, command: PracticePublish) -> None:
    await exchange.publish(
        aio_pika.Message(
            command.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(command.message_id),
            type=command.type,
        ),
        routing_key=COMMAND_QUEUE,
        mandatory=True,
    )


async def _receive(store: QCStore, message: AbstractIncomingMessage) -> None:
    try:
        if message.routing_key == RESULT_KEY:
            await store.apply_result(PracticeResult.model_validate_json(message.body))
        elif message.routing_key == EVENT_QUEUE:
            await store.apply_recheck(PracticeRecheck.model_validate_json(message.body))
        else:
            raise ValueError("unexpected routing key")
    except (ValidationError, ValueError):
        qc_event("qc.message.receive", "rejected", error_code="invalid_event")
        await message.reject(requeue=False)
    except Exception:
        qc_event("qc.message.receive", "retry", error_code="event_processing_failed")
        await asyncio.sleep(1)
        await message.nack(requeue=True)
    else:
        await message.ack()


async def broker_session(store: QCStore, url: str) -> None:
    async with await aio_pika.connect_robust(url, timeout=5) as connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=1)
        exchange = await channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        # Очереди должны существовать и до первого запуска Knowledge
        events = (await declare_topology(channel, exchange))[EVENT_QUEUE]
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_consume(store, events))
            tasks.create_task(_dispatch(store, exchange))


async def _consume(store: QCStore, events: AbstractQueue) -> None:
    async with events.iterator() as messages:
        async for message in messages:
            await _receive(store, message)
    raise ConnectionError("event iterator closed")


async def _dispatch(store: QCStore, exchange: AbstractExchange) -> None:
    async def publish(command: PracticePublish) -> None:
        await _send(exchange, command)

    while True:
        if not await store.dispatch_one(publish):
            await asyncio.sleep(0.5)


async def _retry_broker(store: QCStore, url: str) -> None:
    while True:
        try:
            await broker_session(store, url)
        except Exception:
            qc_event("qc.broker", "retry", error_code="broker_unavailable")
            await asyncio.sleep(5)


async def _prune(analytics: Analytics) -> None:
    while True:
        try:
            await analytics.prune()
        except Exception:
            qc_event("qc.analytics.retention", "retry", error_code="analytics_unavailable")
        await asyncio.sleep(3600)


@asynccontextmanager
async def qc_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = BackendSettings()
    pool = db_manager.lg_pool
    if pool is None:
        raise RuntimeError("Aegra database is unavailable")
    store = QCStore(pool)
    await store.initialize()
    analytics = Analytics(store, retention_days=settings.AUDIT_RETENTION_DAYS)
    app.state.qc_store = store
    app.state.qc_analytics = analytics

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_prune(analytics))]
        if url := settings.RABBITMQ_URL.get_secret_value():
            tasks.append(group.create_task(_retry_broker(store, url)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
