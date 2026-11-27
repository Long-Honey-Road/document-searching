"""Прогресс одной попытки индексации."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic

from regagent_common.contracts import IndexJob

from .registry import DocumentRecord, Registry

log = logging.getLogger(__name__)


@dataclass
class JobProgress:
    job: IndexJob
    registry: Registry
    attempt: int | None = None
    current_step: str = "prepare"
    steps: dict[str, float] = field(default_factory=dict)
    record: DocumentRecord | None = None

    def event(self, step: str, event: str, elapsed: float) -> None:
        log.info(
            "job=%s doc=%s v=%d step=%s %s %.3fs",
            self.job.job_id,
            self.job.doc_id,
            self.job.version,
            step,
            event,
            elapsed,
        )

    async def update(self, current_step: str | None) -> None:
        if self.attempt is not None:
            await self.registry.update_job_progress(
                self.job.job_id, self.attempt, current_step, self.steps
            )

    @asynccontextmanager
    async def step(self, name: str) -> AsyncIterator[None]:
        self.current_step = name
        await self.update(name)
        start = monotonic()
        self.event(name, "start", 0)
        try:
            yield
        except BaseException:
            self.steps[name] = monotonic() - start
            self.event(name, "failed", self.steps[name])
            raise
        else:
            self.steps[name] = monotonic() - start
            self.event(name, "done", self.steps[name])
            await self.update("prepare")
            self.current_step = "prepare"

    async def fail_document(self, error: str) -> None:
        if self.record is not None:
            await self.registry.transition_status(self.record, "failed", error)
