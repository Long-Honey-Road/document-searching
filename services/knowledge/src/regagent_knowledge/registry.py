"""Реестр документов в БД ``knowledge`` и планирование задач индексации.

Реестр — источник истины для проверки прав в поиске: ``level_of(doc_id)`` (слой 4 из
dev_docs/07-security.md). Поиску доступен только подтверждённый индексированный документ.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any, Literal, get_args

import asyncpg
from regagent_common.contracts import IndexJob

from .manifest import ManifestEntry

DocStatus = Literal["queued", "indexing", "indexed", "failed", "invalid_manifest", "deleted"]
STATUSES: frozenset[str] = frozenset(get_args(DocStatus))
INTERRUPTED = "индексация прервана перезапуском knowledge"


@dataclass(frozen=True)
class DocumentRecord:
    doc_id: str
    version: int | None
    sha256: str | None
    level: str | None
    areas: list[str]
    title: str | None
    status: DocStatus
    error: str | None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class JobState:
    status: str
    attempts: int
    current_step: str | None = None
    steps: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    op: str
    doc_id: str
    version: int
    status: str
    attempts: int
    error: str | None
    current_step: str | None
    steps: dict[str, float]
    created_at: datetime
    updated_at: datetime


_JOB_COLUMNS = (
    "job_id, op, doc_id, version, status, attempts, error, current_step, steps, "
    "created_at, updated_at"
)


def _job(row: Mapping[str, Any]) -> JobRecord:
    return JobRecord(
        row["job_id"],
        row["op"],
        row["doc_id"],
        row["version"],
        row["status"],
        row["attempts"],
        row["error"],
        row["current_step"],
        json.loads(row["steps"]),
        row["created_at"],
        row["updated_at"],
    )


def plan_jobs(
    entries: Iterable[ManifestEntry],
    file_hashes: Mapping[str, str],
    records: Iterable[DocumentRecord],
) -> list[IndexJob]:
    """Сравнивает валидные записи манифеста и хэши файлов с реестром.

    - нет записи в реестре, другая версия, другой sha, другой уровень, области или
      название → upsert (уровень и метаданные зашиты в заголовки фрагментов);
    - записи манифеста больше нет, а документ в индексе → delete;
    - файла нет → задачи нет (ошибку покажет сверка папки);
    - упавший документ с тем же входом не перезапускается сам — только вручную.
    """
    by_id = {r.doc_id: r for r in records}
    jobs: list[IndexJob] = []
    seen: set[str] = set()
    for e in entries:
        seen.add(e.doc_id)
        sha = file_hashes.get(e.file)
        if sha is None:
            continue
        rec = by_id.get(e.doc_id)
        unchanged = (
            rec is not None
            and rec.status in {"indexed", "failed"}
            and rec.version == e.version
            and rec.sha256 == sha
            and rec.level == e.access
            and rec.areas == list(e.areas)
            and rec.title == e.title
        )
        if not unchanged:
            jobs.append(
                IndexJob(op="upsert", doc_id=e.doc_id, version=e.version, path=e.file, sha256=sha)
            )
    for rec in by_id.values():
        if rec.doc_id in seen or rec.status == "deleted" or rec.version is None:
            continue
        jobs.append(IndexJob(op="delete", doc_id=rec.doc_id, version=rec.version))
    return jobs


def _record(row: Mapping[str, Any]) -> DocumentRecord:
    return DocumentRecord(
        doc_id=row["doc_id"],
        version=row["version"],
        sha256=row["sha256"],
        level=row["level"],
        areas=list(row["areas"] or []),
        title=row["title"],
        status=row["status"],
        error=row["error"],
        updated_at=row["updated_at"],
    )


class Registry:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def migrate(self) -> None:
        sql = (
            resources.files(__package__)
            .joinpath("migrations/001_registry.sql")
            .read_text(encoding="utf-8")
        )
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def get(self, doc_id: str) -> DocumentRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM documents WHERE doc_id = $1", doc_id)
        return _record(row) if row else None

    async def all(self) -> list[DocumentRecord]:
        rows = await self._pool.fetch("SELECT * FROM documents ORDER BY doc_id")
        return [_record(r) for r in rows]

    async def set_status(
        self,
        doc_id: str,
        version: int | None,
        sha256: str | None,
        level: str | None,
        status: str,
        error: str | None = None,
        *,
        areas: list[str] | None = None,
        title: str | None = None,
    ) -> None:
        """``areas``/``title`` = ``None`` — оставить прежние."""
        if status not in STATUSES:
            raise ValueError(f"неизвестный статус документа: {status!r}")
        await self._pool.execute(
            """
            INSERT INTO documents (doc_id, version, sha256, level, areas, title, status, error)
            VALUES ($1, $2, $3, $4, COALESCE($5, '{}'::text[]), $6, $7, $8)
            ON CONFLICT (doc_id) DO UPDATE SET
                version = EXCLUDED.version,
                sha256 = EXCLUDED.sha256,
                level = EXCLUDED.level,
                areas = COALESCE($5, documents.areas),
                title = COALESCE($6, documents.title),
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                updated_at = now()
            """,
            doc_id,
            version,
            sha256,
            level,
            areas,
            title,
            status,
            error,
        )

    async def level_of(self, doc_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT level, status FROM documents WHERE doc_id = $1", doc_id
        )
        if row is None or row["status"] != "indexed":
            return None
        level: str | None = row["level"]
        return level

    async def transition_status(
        self, record: DocumentRecord, status: DocStatus, error: str | None = None
    ) -> bool:
        """Меняет статус только у той записи, которую прочитал вызывающий код."""
        result: str = await self._pool.execute(
            """
            UPDATE documents SET status = $8, error = $9, updated_at = now()
            WHERE doc_id = $1 AND version IS NOT DISTINCT FROM $2
              AND sha256 IS NOT DISTINCT FROM $3 AND level IS NOT DISTINCT FROM $4
              AND areas = $5 AND title IS NOT DISTINCT FROM $6 AND status = $7
            """,
            record.doc_id,
            record.version,
            record.sha256,
            record.level,
            record.areas,
            record.title,
            record.status,
            status,
            error,
        )
        return result == "UPDATE 1"

    async def job_state(self, job_id: str) -> JobState | None:
        row = await self._pool.fetchrow(
            "SELECT status, attempts, current_step, steps FROM jobs WHERE job_id = $1", job_id
        )
        if row is None:
            return None
        return JobState(
            row["status"], row["attempts"], row["current_step"], json.loads(row["steps"])
        )

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
        await self._pool.execute(
            """
            INSERT INTO jobs (job_id, op, doc_id, version, sha256, path, status, attempts,
                              error, steps, created_at, current_step)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (job_id) DO UPDATE SET
                status = EXCLUDED.status, attempts = EXCLUDED.attempts, error = EXCLUDED.error,
                steps = EXCLUDED.steps, current_step = EXCLUDED.current_step, updated_at = now()
            """,
            job.job_id,
            job.op,
            job.doc_id,
            job.version,
            job.sha256,
            job.path,
            status,
            attempts,
            error,
            json.dumps(steps or {}),
            job.created_at,
            current_step,
        )

    async def list_jobs(self, *, status: str | None, limit: int) -> list[JobRecord]:
        """Задачи, свежие первыми; ``status=None`` — все."""
        rows = await self._pool.fetch(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE $1::text IS NULL OR status = $1 "  # noqa: S608
            "ORDER BY created_at DESC, job_id LIMIT $2",
            status,
            limit,
        )
        return [_job(row) for row in rows]

    async def get_job(self, job_id: str) -> JobRecord | None:
        row = await self._pool.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = $1",  # noqa: S608
            job_id,
        )
        return _job(row) if row is not None else None

    async def interrupt_indexing(self) -> list[str]:
        """При старте knowledge: ни один документ и задача не остаются в indexing (D-50).

        Документ — в failed: его переиндексирует повторная доставка сообщения или
        ``reindex --failed``. Задача — в retrying: повторная доставка засчитает падение как
        неудачную попытку, и документ, роняющий knowledge, уйдёт в DLQ."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE documents SET status = 'failed', error = $1, updated_at = now()
                WHERE status = 'indexing' RETURNING doc_id
                """,
                INTERRUPTED,
            )
            await conn.execute(
                """
                UPDATE jobs SET status = 'retrying', error = $1, current_step = NULL,
                    updated_at = now()
                WHERE status = 'indexing'
                """,
                INTERRUPTED,
            )
        return sorted(row["doc_id"] for row in rows)

    async def update_job_progress(
        self, job_id: str, attempt: int, current_step: str | None, steps: Mapping[str, float]
    ) -> None:
        await self._pool.execute(
            """
            UPDATE jobs SET current_step = $3, steps = $4, updated_at = now()
            WHERE job_id = $1 AND attempts = $2 AND status = 'indexing'
            """,
            job_id,
            attempt,
            current_step,
            json.dumps(steps),
        )
