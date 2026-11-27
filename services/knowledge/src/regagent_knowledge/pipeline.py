"""Синхронизация одной редакции между источником, OKF, LightRAG и реестром."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Protocol

from lightrag.utils import Tokenizer
from regagent_common.access import AccessLevels
from regagent_common.contracts import IndexJob

from .manifest import ManifestEntry
from .normalize import Clause, build_chunks, split_clauses
from .okf import OkfStore
from .parsing import Parser
from .registry import DocumentRecord, Registry
from .settings import KnowledgeSettings
from .watcher import FolderSource


class IndexStorage(Protocol):
    async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None: ...

    async def delete(self, level: str, doc_key: str) -> None: ...


@dataclass(frozen=True)
class PipelineDeps:
    parser: Parser
    okf: OkfStore
    rag: IndexStorage
    registry: Registry
    settings: KnowledgeSettings
    levels: AccessLevels | None = None
    tokenizer: Tokenizer | None = None


def _upsert_snapshot(source: FolderSource, job: IndexJob) -> tuple[ManifestEntry, bytes] | None:
    entry = source.manifest().entries.get(job.doc_id)
    if entry is None or entry.version != job.version or entry.file != job.path:
        return None
    try:
        data = source.snapshot(entry.file)
    except (ValueError, RuntimeError):
        return None
    if data is None or hashlib.sha256(data).hexdigest() != job.sha256:
        return None
    return entry, data


def _parse(parser: Parser, entry: ManifestEntry, data: bytes) -> str:
    with TemporaryDirectory(prefix="regagent-document-") as directory:
        path = Path(directory) / Path(entry.file).name
        path.write_bytes(data)
        return parser.parse(path)


async def _timed[T](name: str, operation: Awaitable[T], steps: dict[str, float]) -> T:
    start = monotonic()
    try:
        return await operation
    finally:
        steps[name] = monotonic() - start


def _matches(record: DocumentRecord, entry: ManifestEntry, sha256: str) -> bool:
    return (
        record.version == entry.version
        and record.sha256 == sha256
        and record.level == entry.access
        and record.areas == entry.areas
        and record.title == entry.title
    )


async def _upsert(
    job: IndexJob, deps: PipelineDeps, source: FolderSource, steps: dict[str, float]
) -> None:
    snap = await asyncio.to_thread(_upsert_snapshot, source, job)
    if snap is None or job.sha256 is None:
        return
    entry, data = snap
    previous = await deps.registry.get(job.doc_id)
    if previous is not None:
        if previous.status == "indexed" and _matches(previous, entry, job.sha256):
            return
        if not await deps.registry.transition_status(previous, "indexing"):
            return
        if (
            previous.version is not None
            and previous.level is not None
            and previous.status != "deleted"
            and (previous.version, previous.level) != (entry.version, entry.access)
        ):
            await _timed(
                "delete_previous",
                deps.rag.delete(previous.level, f"{job.doc_id}@v{previous.version}"),
                steps,
            )

    await deps.registry.set_status(
        entry.doc_id,
        entry.version,
        job.sha256,
        entry.access,
        "indexing",
        areas=entry.areas,
        title=entry.title,
    )
    target = await deps.registry.get(entry.doc_id)
    markdown = await _timed("parse", asyncio.to_thread(_parse, deps.parser, entry, data), steps)

    def normalize() -> tuple[list[Clause], list[str]]:
        clauses = split_clauses(markdown)
        chunks = build_chunks(
            entry, clauses, deps.settings.chunk_max_tokens, tokenizer=deps.tokenizer
        )
        if not chunks:
            raise ValueError("в документе нет фрагментов")
        return clauses, [chunk.text for chunk in chunks]

    clauses, texts = await _timed("normalize", asyncio.to_thread(normalize), steps)
    current = await asyncio.to_thread(_upsert_snapshot, source, job)
    if current is None or current[0] != entry:
        return
    await _timed(
        "okf", asyncio.to_thread(deps.okf.write_regulation, entry, clauses, job.sha256), steps
    )
    key = f"{entry.doc_id}@v{entry.version}"
    await _timed("index", deps.rag.insert(entry.access, key, texts), steps)

    current = await asyncio.to_thread(_upsert_snapshot, source, job)
    if (
        current is not None
        and current[0] == entry
        and target is not None
        and target.status == "indexing"
    ):
        indexed = await _timed(
            "registry", deps.registry.transition_status(target, "indexed"), steps
        )
        if indexed:
            return
    await _timed("remove_stale", deps.rag.delete(entry.access, key), steps)


async def _delete(
    job: IndexJob, deps: PipelineDeps, source: FolderSource, steps: dict[str, float]
) -> None:
    manifest = await asyncio.to_thread(source.manifest)
    if job.doc_id in manifest.entries or manifest.error_for(job.doc_id):
        return
    previous = await deps.registry.get(job.doc_id)
    if previous is None or previous.status == "deleted" or previous.version != job.version:
        return
    if not await deps.registry.transition_status(previous, "indexing"):
        return

    if previous.level is not None:
        await _timed(
            "index", deps.rag.delete(previous.level, f"{job.doc_id}@v{job.version}"), steps
        )
    await _timed(
        "okf", asyncio.to_thread(deps.okf.set_status, job.doc_id, job.version, "deleted"), steps
    )

    current = await asyncio.to_thread(source.manifest)
    target = await deps.registry.get(job.doc_id)
    if (
        job.doc_id not in current.entries
        and current.error_for(job.doc_id) is None
        and target is not None
        and target.status == "indexing"
    ):
        await _timed("registry", deps.registry.transition_status(target, "deleted"), steps)


async def process_job(
    job: IndexJob, deps: PipelineDeps, *, steps: dict[str, float] | None = None
) -> None:
    if steps is None:
        steps = {}
    levels = deps.levels
    if levels is None:
        levels = await asyncio.to_thread(AccessLevels.from_yaml, deps.settings.access_levels_file)
    source = FolderSource(deps.settings.docs_dir, levels)
    try:
        if job.op == "upsert":
            await _upsert(job, deps, source, steps)
        else:
            await _delete(job, deps, source, steps)
    except Exception as exc:
        current = await deps.registry.get(job.doc_id)
        if current is not None and current.status == "indexing":
            await deps.registry.transition_status(current, "failed", str(exc))
        raise
