"""Долговечная очередь index.jobs с подтверждением после обработки."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from types import TracebackType
from typing import Self

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage, AbstractQueue
from pydantic import ValidationError
from regagent_common.contracts import (
    IndexJob,
    PracticePublicationResult,
    PracticePublish,
    PracticeRecheck,
)

from .jobs import JobProgress
from .pipeline import PipelineDeps, process_job
from .practice import PracticeService

log = logging.getLogger(__name__)

_EXCHANGE = "regagent"
_INDEX_JOBS = "index.jobs"
_PRACTICE_PUBLISH = "practice.publish"
_DLQ = "regagent.dlq"


class IndexQueue:
    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: aio_pika.RobustConnection | None = None
        self._exchange: AbstractExchange
        self.jobs: AbstractQueue
        self.practices: AbstractQueue
        self.write_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        connection = await aio_pika.connect_robust(self._url)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.set_qos(prefetch_count=1)
            exchange = await channel.declare_exchange(
                _EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
            )
            dead_letters = await channel.declare_queue(_DLQ, durable=True)
            await dead_letters.bind(exchange, routing_key=_DLQ)
            jobs = await channel.declare_queue(
                _INDEX_JOBS,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": _EXCHANGE,
                    "x-dead-letter-routing-key": _DLQ,
                },
            )
            await jobs.bind(exchange, routing_key=_INDEX_JOBS)
            self.practices = await channel.declare_queue(
                _PRACTICE_PUBLISH,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": _EXCHANGE,
                    "x-dead-letter-routing-key": _DLQ,
                },
            )
            await self.practices.bind(exchange, routing_key=_PRACTICE_PUBLISH)
            for key in ("practice.published", "qc.events"):
                event_queue = await channel.declare_queue(key, durable=True)
                await event_queue.bind(exchange, routing_key=key)
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        self._exchange = exchange
        self.jobs = jobs
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def publish(self, job: IndexJob | PracticePublicationResult | PracticeRecheck) -> None:
        await self._exchange.publish(
            aio_pika.Message(
                job.model_dump_json().encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=job.message_id,
                type=job.type,
            ),
            routing_key=job.type,
        )


async def _attempt(job: IndexJob, deps: PipelineDeps, progress: JobProgress) -> None:
    deadline = asyncio.timeout(deps.settings.index_job_timeout_s)
    try:
        async with deadline:
            await process_job(job, deps, progress=progress)
    except TimeoutError:
        if not deadline.expired():
            raise
        error = f"превышено время задачи (шаг {progress.current_step})"
        await progress.fail_document(error)
        raise TimeoutError(error) from None


async def _process_message(
    message: AbstractIncomingMessage,
    deps: PipelineDeps,
    after_index: Callable[[], Awaitable[None]] | None = None,
) -> None:
    async with message.process(requeue=True, ignore_processed=True):
        try:
            job = IndexJob.model_validate_json(message.body)
        except ValidationError:
            log.warning("некорректное сообщение в %s", _INDEX_JOBS)
            await message.reject(requeue=False)
            return

        state = await deps.registry.job_state(job.job_id)
        if state is not None:
            if state.status == "succeeded":
                return
            if state.status == "failed":
                await message.reject(requeue=False)
                return
        attempt = 1 if state is None else state.attempts + int(state.status != "indexing")
        while True:
            progress = JobProgress(job, deps.registry, attempt)
            await deps.registry.save_job(
                job, "indexing", attempt, current_step=progress.current_step
            )
            start = monotonic()
            progress.event("job", "start", 0)
            try:
                await _attempt(job, deps, progress)
                if after_index is not None:
                    await after_index()
            except Exception as exc:
                error = str(exc)
                exhausted = attempt > deps.settings.index_max_retries
                status = "failed" if exhausted else "retrying"
                await deps.registry.save_job(
                    job, status, attempt, error=error, steps=progress.steps
                )
                progress.event("job", "failed", monotonic() - start)
                log.warning("задача %s, попытка %d: %s", job.job_id, attempt, error)
                if exhausted:
                    await message.reject(requeue=False)
                    return
                await asyncio.sleep(attempt)
                attempt += 1
            else:
                await deps.registry.save_job(job, "succeeded", attempt, steps=progress.steps)
                progress.event("job", "done", monotonic() - start)
                return


async def run_consumer(
    queue: IndexQueue, deps: PipelineDeps, practices: PracticeService | None = None
) -> None:
    async def review() -> None:
        if practices is not None:
            await practices.review_changes(queue.publish)

    async with queue.jobs.iterator() as messages:
        async for message in messages:
            try:
                async with queue.write_lock:
                    await _process_message(message, deps, review)
            except Exception:
                log.exception("не удалось подтвердить обработку задачи %s", message.message_id)
                await asyncio.sleep(1)


async def _process_practice_message(
    message: AbstractIncomingMessage,
    queue: IndexQueue,
    service: PracticeService,
    *,
    retries: int,
    timeout_s: float,
) -> None:
    async with message.process(requeue=True, ignore_processed=True):
        try:
            command = PracticePublish.model_validate_json(message.body)
        except ValidationError:
            log.warning("event=practice_publication status=failed error_code=invalid_message")
            await message.reject(requeue=False)
            return

        started = monotonic()
        for attempt in range(1, retries + 2):
            try:
                async with queue.write_lock, asyncio.timeout(timeout_s):
                    result = await service.publish(command)
            except Exception as exc:
                log.warning(
                    "event=practice_publication status=%s ticket_id=%s practice_id=%s "
                    "attempt=%d duration_ms=%d error_code=%s",
                    "retrying" if attempt <= retries else "failed",
                    command.ticket_id,
                    command.practice_id,
                    attempt,
                    (monotonic() - started) * 1000,
                    type(exc).__name__,
                )
                if attempt <= retries:
                    await asyncio.sleep(attempt)
                    continue
                result = PracticePublicationResult(
                    ticket_id=command.ticket_id,
                    practice_id=command.practice_id,
                    revision=command.revision,
                    status="failed",
                    error_code="publication_unavailable",
                )
            await queue.publish(result)
            return


async def run_practice_consumer(
    queue: IndexQueue, service: PracticeService, *, retries: int, timeout_s: float
) -> None:
    async with queue.practices.iterator() as messages:
        async for message in messages:
            try:
                await _process_practice_message(
                    message, queue, service, retries=retries, timeout_s=timeout_s
                )
            except Exception as exc:
                log.warning(
                    "event=practice_delivery status=failed error_code=%s", type(exc).__name__
                )
                await asyncio.sleep(1)
