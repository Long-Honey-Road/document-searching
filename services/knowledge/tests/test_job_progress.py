import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import cast

import pytest
from aio_pika.abc import AbstractIncomingMessage
from regagent_common.contracts import IndexJob
from regagent_knowledge.consumer import _process_message
from regagent_knowledge.jobs import IndexRequest
from regagent_knowledge.pipeline import PipelineDeps
from regagent_knowledge.registry import JobState
from regagent_knowledge.reindex import queue_reindex
from regagent_knowledge.watcher import FolderSource, scan

from .test_pipeline import LEVELS, Folder, MemoryRegistry, Rag
from .test_pipeline import environment as environment


class MemoryJobs(MemoryRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: dict[str, JobState] = {}
        self.history: list[str] = []
        self.errors: dict[str, str | None] = {}

    async def job_state(self, job_id: str) -> JobState | None:
        return self.jobs.get(job_id)

    async def save_job(
        self,
        job: IndexJob,
        status: str,
        attempts: int,
        *,
        error: str | None = None,
        steps: Mapping[str, float] | None = None,
        current_step: str | None = None,
    ) -> None:
        self.jobs[job.job_id] = JobState(status, attempts, current_step, dict(steps or {}))
        self.errors[job.job_id] = error
        self.history.append(status)

    async def update_job_progress(
        self, job_id: str, attempt: int, current_step: str | None, steps: Mapping[str, float]
    ) -> None:
        assert self.jobs[job_id].attempts == attempt
        self.jobs[job_id] = JobState("indexing", attempt, current_step, dict(steps))


class Message:
    def __init__(self, job: IndexJob) -> None:
        self.body = job.model_dump_json().encode()
        self.dead_lettered = False

    @asynccontextmanager
    async def process(self, *, requeue: bool, ignore_processed: bool) -> AsyncIterator[None]:
        assert requeue and ignore_processed
        yield

    async def reject(self, *, requeue: bool) -> None:
        assert not requeue
        self.dead_lettered = True


async def test_timeout_retries_then_dlq_and_next_job_succeeds(
    environment: tuple[Folder, PipelineDeps, Rag], caplog: pytest.LogCaptureFixture
) -> None:
    folder, deps, _ = environment
    registry = MemoryJobs()
    index_started = asyncio.Event()

    class StuckRag(Rag):
        async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None:
            if doc_key == "a@v3":
                index_started.set()
                await asyncio.Event().wait()
            await super().insert(level, doc_key, chunks)

    deps = replace(
        deps,
        rag=StuckRag(),
        registry=registry,
        settings=deps.settings.model_copy(
            update={"index_job_timeout_s": 0.5, "index_max_retries": 1}
        ),
    )
    (job,) = await scan(folder.path, LEVELS, registry)
    message = Message(job)
    with caplog.at_level(logging.INFO, logger="regagent_knowledge.jobs"):
        processing = asyncio.create_task(
            _process_message(cast(AbstractIncomingMessage, message), deps)
        )
        try:
            await asyncio.wait_for(index_started.wait(), 5)
            state = registry.jobs[job.job_id]
            assert state.current_step == "index"
            assert {"parse", "normalize", "okf"} <= state.steps.keys()
            assert "index" not in state.steps
            await asyncio.wait_for(processing, 5)
        finally:
            if not processing.done():
                processing.cancel()
                await asyncio.gather(processing, return_exceptions=True)

    assert message.dead_lettered
    assert registry.history == ["indexing", "retrying", "indexing", "failed"]
    record = await registry.get(job.doc_id)
    assert record is not None and record.status == "failed"
    assert record.error == "превышено время задачи (шаг index)"
    assert registry.errors[job.job_id] == record.error
    assert f"job={job.job_id} doc=a v=3 step=index start 0.000s" in caplog.text
    assert f"job={job.job_id} doc=a v=3 step=okf done" in caplog.text

    folder.manifest(4)
    folder.document("1. Новая редакция")
    (following,) = await scan(folder.path, LEVELS, registry)
    await _process_message(cast(AbstractIncomingMessage, Message(following)), deps)
    assert registry.jobs[following.job_id].status == "succeeded"
    record = await registry.get(job.doc_id)
    assert record is not None and record.status == "indexed"


async def test_reindex_queues_current_document_and_deduplicates_delivery(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, rag = environment
    registry = MemoryJobs()
    deps = replace(deps, registry=registry)
    (original,) = await scan(folder.path, LEVELS, registry)
    await _process_message(cast(AbstractIncomingMessage, Message(original)), deps)
    published: list[IndexJob] = []

    class Publisher:
        async def publish(self, job: IndexJob) -> None:
            published.append(job)

    assert await queue_reindex(FolderSource(folder.path, LEVELS), registry, Publisher()) == 1
    [forced] = published
    assert isinstance(forced, IndexRequest) and forced.force
    assert forced.sha256 == original.sha256 and forced.job_id != original.job_id
    await _process_message(cast(AbstractIncomingMessage, Message(forced)), deps)
    await _process_message(cast(AbstractIncomingMessage, Message(forced)), deps)
    assert rag.calls == [("insert", "public", "a@v3")] * 2
    assert not await scan(folder.path, LEVELS, registry)
