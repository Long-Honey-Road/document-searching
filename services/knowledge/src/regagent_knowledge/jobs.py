"""Прогресс одной попытки индексации."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic

from regagent_common.contracts import IndexJob

from .events import events
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

    def _emit_step(self, name: str, status: str, error_code: str | None) -> None:
        events.emit(
            "index.step",
            status=status,
            duration_ms=self.steps[name] * 1000,
            error_code=error_code,
            step=name,
            job_id=self.job.job_id,
            doc_id=self.job.doc_id,
            version=self.job.version,
            attempt=self.attempt,
        )

    def emit_result(self, status: str, seconds: float, error_code: str | None = None) -> None:
        """Итог попытки: ``succeeded``, ``retrying`` или ``failed``."""
        events.emit(
            "index.job",
            status=status,
            duration_ms=seconds * 1000,
            error_code=error_code,
            job_id=self.job.job_id,
            doc_id=self.job.doc_id,
            version=self.job.version,
            op=self.job.op,
            attempt=self.attempt,
            force=self.job.force,
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
        except BaseException as exc:
            self.steps[name] = monotonic() - start
            self.event(name, "failed", self.steps[name])
            self._emit_step(name, "error", type(exc).__name__)
            raise
        else:
            self.steps[name] = monotonic() - start
            self.event(name, "done", self.steps[name])
            self._emit_step(name, "ok", None)
            await self.update("prepare")
            self.current_step = "prepare"

    async def fail_document(self, error: str) -> None:
        if self.record is not None:
            await self.registry.transition_status(self.record, "failed", error)
