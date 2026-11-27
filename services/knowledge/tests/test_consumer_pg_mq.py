import asyncio
import os
from collections.abc import Sequence
from dataclasses import replace

import aio_pika
import pytest
from regagent_common.contracts import IndexJob
from regagent_knowledge.consumer import IndexQueue, run_consumer
from regagent_knowledge.pipeline import PipelineDeps
from regagent_knowledge.registry import Registry
from regagent_knowledge.watcher import scan

from .test_pipeline import LEVELS, Folder, Rag
from .test_pipeline import environment as environment
from .test_registry_pg import registry as registry

pytestmark = pytest.mark.integration


async def test_failed_job_is_retried_and_dead_lettered(
    environment: tuple[Folder, PipelineDeps, Rag], registry: Registry
) -> None:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.fail("для integration-теста нужен TEST_RABBITMQ_URL отдельного брокера")
    folder, deps, _ = environment
    folder.document("")
    deps = replace(
        deps, registry=registry, settings=deps.settings.model_copy(update={"index_max_retries": 1})
    )
    (job,) = await scan(folder.path, LEVELS, registry)

    async with IndexQueue(url) as queue, await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        dead_letters = await channel.get_queue("regagent.dlq")
        async with dead_letters.iterator() as messages:
            consumer = asyncio.create_task(run_consumer(queue, deps))
            try:
                await queue.publish(job)
                delivered = await asyncio.wait_for(anext(messages), 10)
                async with delivered.process():
                    assert IndexJob.model_validate_json(delivered.body).job_id == job.job_id
            finally:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

    state = await registry.job_state(job.job_id)
    assert state is not None and (state.status, state.attempts) == ("failed", 2)
    record = await registry.get(job.doc_id)
    assert record is not None and record.status == "failed"


async def test_running_job_exposes_current_step_and_completed_timings(
    environment: tuple[Folder, PipelineDeps, Rag], registry: Registry
) -> None:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.fail("для integration-теста нужен TEST_RABBITMQ_URL отдельного брокера")
    folder, deps, _ = environment
    started = asyncio.Event()
    release = asyncio.Event()

    class PausedRag(Rag):
        async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None:
            started.set()
            await release.wait()
            await super().insert(level, doc_key, chunks)

    deps = replace(deps, registry=registry, rag=PausedRag())
    (job,) = await scan(folder.path, LEVELS, registry)
    async with IndexQueue(url) as queue:
        consumer = asyncio.create_task(run_consumer(queue, deps))
        try:
            await queue.publish(job)
            await asyncio.wait_for(started.wait(), 8)
            running = await registry.job_state(job.job_id)
            assert running is not None and running.current_step == "index"
            assert {"parse", "normalize", "okf"} <= running.steps.keys()
            release.set()
            async with asyncio.timeout(5):
                while (  # noqa: ASYNC110 — ждём фиксацию состояния в PostgreSQL
                    state := await registry.job_state(job.job_id)
                ) and state.status == "indexing":
                    await asyncio.sleep(0.02)
            assert state is not None and state.status == "succeeded"
            assert state.current_step is None and "index" in state.steps
        finally:
            release.set()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
