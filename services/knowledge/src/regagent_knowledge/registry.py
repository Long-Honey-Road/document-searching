"""Реестр документов в БД ``knowledge`` и планирование задач индексации.

Реестр — источник истины для проверки прав в поиске: ``level_of(doc_id)`` (слой 4 из
dev_docs/07-security.md). У удалённого и невалидного документа уровня нет — его фрагменты
в выдачу не попадают.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal, get_args

import asyncpg
from regagent_common.contracts import IndexJob

from .manifest import ManifestEntry

DocStatus = Literal["queued", "indexing", "indexed", "failed", "invalid_manifest", "deleted"]
STATUSES: frozenset[str] = frozenset(get_args(DocStatus))
# Статусы, при которых в индексе нет фрагментов документа: уровень не отдаётся.
_NO_CONTENT: frozenset[str] = frozenset({"deleted", "invalid_manifest"})


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
            and rec.status not in _NO_CONTENT
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
        if rec.doc_id in seen or rec.status in _NO_CONTENT or rec.version is None:
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
        if row is None or row["status"] in _NO_CONTENT:
            return None
        level: str | None = row["level"]
        return level
