import asyncio
import os
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
