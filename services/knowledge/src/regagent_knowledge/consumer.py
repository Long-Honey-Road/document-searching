"""Долговечная очередь index.jobs с подтверждением после обработки."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from types import TracebackType
from typing import Self

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage, AbstractQueue
from pydantic import ValidationError
from regagent_common.contracts import IndexJob

from .jobs import JobProgress
from .pipeline import PipelineDeps, process_job

log = logging.getLogger(__name__)

_EXCHANGE = "regagent"
_INDEX_JOBS = "index.jobs"
_DLQ = "regagent.dlq"


class IndexQueue:
    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: aio_pika.RobustConnection | None = None
        self._exchange: AbstractExchange
        self.jobs: AbstractQueue

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

    async def publish(self, job: IndexJob) -> None:
        await self._exchange.publish(
            aio_pika.Message(
                job.model_dump_json().encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=job.message_id,
                type=job.type,
            ),
            routing_key=_INDEX_JOBS,
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


async def _process_message(message: AbstractIncomingMessage, deps: PipelineDeps) -> None:
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
            except Exception as exc:
                error = str(exc)
                exhausted = attempt > deps.settings.index_max_retries
                status = "failed" if exhausted else "retrying"
                await deps.registry.save_job(
                    job, status, attempt, error=error, steps=progress.steps
                )
                progress.event("job", "failed", monotonic() - start)
                progress.emit_result(status, monotonic() - start, type(exc).__name__)
                log.warning("задача %s, попытка %d: %s", job.job_id, attempt, error)
                if exhausted:
                    await message.reject(requeue=False)
                    return
                await asyncio.sleep(attempt)
                attempt += 1
            else:
                await deps.registry.save_job(job, "succeeded", attempt, steps=progress.steps)
                progress.event("job", "done", monotonic() - start)
                progress.emit_result("succeeded", monotonic() - start)
                return


async def run_consumer(queue: IndexQueue, deps: PipelineDeps) -> None:
    async with queue.jobs.iterator() as messages:
        async for message in messages:
            try:
                await _process_message(message, deps)
            except Exception:
                log.exception("не удалось подтвердить обработку задачи %s", message.message_id)
                await asyncio.sleep(1)
