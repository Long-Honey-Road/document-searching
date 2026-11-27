"""Синхронизация одной редакции между источником, OKF, LightRAG и реестром."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from lightrag.utils import Tokenizer
from regagent_common.access import AccessLevels
from regagent_common.contracts import IndexJob

from .jobs import JobProgress
from .manifest import ManifestEntry
from .normalize import Clause, build_chunks, split_clauses
from .okf import OkfStore
from .parsing import Parser
from .references import DocumentReferences, find_document_references
from .registry import DocumentRecord, Registry
from .settings import KnowledgeSettings
from .watcher import FolderSource

log = logging.getLogger(__name__)


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


def _upsert_snapshot(
    source: FolderSource, job: IndexJob
) -> tuple[ManifestEntry, bytes, dict[str, ManifestEntry]] | None:
    entries = source.manifest().entries
    entry = entries.get(job.doc_id)
    if entry is None or entry.version != job.version or entry.file != job.path:
        return None
    try:
        data = source.snapshot(entry.file)
    except (ValueError, RuntimeError):
        return None
    if data is None or hashlib.sha256(data).hexdigest() != job.sha256:
        return None
    return entry, data, entries


def _parse(parser: Parser, entry: ManifestEntry, data: bytes) -> str:
    with TemporaryDirectory(prefix="regagent-document-") as directory:
        path = Path(directory) / Path(entry.file).name
        path.write_bytes(data)
        return parser.parse(path)


def _matches(record: DocumentRecord, entry: ManifestEntry, sha256: str) -> bool:
    return (
        record.version == entry.version
        and record.sha256 == sha256
        and record.level == entry.access
        and record.areas == entry.areas
        and record.title == entry.title
    )


async def _upsert(
    job: IndexJob, deps: PipelineDeps, source: FolderSource, progress: JobProgress
) -> None:
    async with progress.step("snapshot"):
        snap = await asyncio.to_thread(_upsert_snapshot, source, job)
    if snap is None or job.sha256 is None:
        return
    entry, data, entries = snap
    previous = await deps.registry.get(job.doc_id)
    if previous is not None:
        force = job.force
        if not force and previous.status == "indexed" and _matches(previous, entry, job.sha256):
            return
        if not await deps.registry.transition_status(previous, "indexing"):
            return
        progress.record = replace(previous, status="indexing", error=None)
        if (
            previous.version is not None
            and previous.level is not None
            and previous.status != "deleted"
            and (previous.version, previous.level) != (entry.version, entry.access)
        ):
            async with progress.step("delete_previous"):
                await deps.rag.delete(previous.level, f"{job.doc_id}@v{previous.version}")

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
    if target is None or target.status != "indexing" or not _matches(target, entry, job.sha256):
        return
    progress.record = target
    async with progress.step("parse"):
        markdown = await asyncio.to_thread(_parse, deps.parser, entry, data)

    def normalize() -> tuple[list[Clause], list[str], DocumentReferences]:
        clauses = split_clauses(markdown)
        chunks = build_chunks(
            entry, clauses, deps.settings.chunk_max_tokens, tokenizer=deps.tokenizer
        )
        if not chunks:
            raise ValueError("в документе нет фрагментов")
        references = find_document_references(entry.doc_id, clauses, entries.values())
        return clauses, [chunk.text for chunk in chunks], references

    async with progress.step("normalize"):
        clauses, texts, references = await asyncio.to_thread(normalize)
    async with progress.step("validate_snapshot"):
        current = await asyncio.to_thread(_upsert_snapshot, source, job)
    if current is None or current[0] != entry:
        return
    if current[2] != entries:
        references = await asyncio.to_thread(
            find_document_references, entry.doc_id, clauses, current[2].values()
        )
    async with progress.step("okf"):
        await asyncio.to_thread(
            deps.okf.write_regulation, entry, clauses, job.sha256, references=references
        )
    key = f"{entry.doc_id}@v{entry.version}"
    async with progress.step("index"):
        await deps.rag.insert(entry.access, key, texts)

    async with progress.step("registry"):
        current = await asyncio.to_thread(_upsert_snapshot, source, job)
        if (
            current is not None
            and current[0] == entry
            and await deps.registry.transition_status(target, "indexed")
        ):
            return
    async with progress.step("remove_stale"):
        await deps.rag.delete(entry.access, key)


async def _delete(
    job: IndexJob, deps: PipelineDeps, source: FolderSource, progress: JobProgress
) -> None:
    async with progress.step("snapshot"):
        manifest = await asyncio.to_thread(source.manifest)
    if job.doc_id in manifest.entries or manifest.error_for(job.doc_id):
        return
    previous = await deps.registry.get(job.doc_id)
    if previous is None or previous.status == "deleted" or previous.version != job.version:
        return
    if not await deps.registry.transition_status(previous, "indexing"):
        return
    target = replace(previous, status="indexing", error=None)
    progress.record = target

    if previous.level is not None:
        async with progress.step("index"):
            await deps.rag.delete(previous.level, f"{job.doc_id}@v{job.version}")
    async with progress.step("okf"):
        await asyncio.to_thread(deps.okf.set_status, job.doc_id, job.version, "deleted")

    async with progress.step("registry"):
        current = await asyncio.to_thread(source.manifest)
        if job.doc_id not in current.entries and current.error_for(job.doc_id) is None:
            await deps.registry.transition_status(target, "deleted")


async def process_job(
    job: IndexJob,
    deps: PipelineDeps,
    *,
    steps: dict[str, float] | None = None,
    progress: JobProgress | None = None,
) -> None:
    progress = progress or JobProgress(job, deps.registry, steps=steps if steps is not None else {})
    levels = deps.levels
    if levels is None:
        levels = await asyncio.to_thread(AccessLevels.from_yaml, deps.settings.access_levels_file)
    source = FolderSource(deps.settings.docs_dir, levels)
    try:
        if job.op == "upsert":
            await _upsert(job, deps, source, progress)
        else:
            await _delete(job, deps, source, progress)
    except Exception as exc:
        await progress.fail_document(str(exc))
        raise


class _Leftovers(Protocol):
    async def discard_unfinished(self) -> dict[str, list[str]]: ...


class _Interruptible(Protocol):
    async def interrupt_indexing(self) -> list[str]: ...


async def recover_after_restart(rag: _Leftovers, registry: _Interruptible) -> None:
    """До запуска consumer: убрать из LightRAG недоделанные вставки и снять indexing в
    реестре (D-50). Порядок важен: реестр — источник истины, LightRAG чистим первым."""
    if discarded := await rag.discard_unfinished():
        listed = "; ".join(f"{level}: {', '.join(keys)}" for level, keys in discarded.items())
        log.warning("удалены недоделанные вставки LightRAG — %s", listed)
    if interrupted := await registry.interrupt_indexing():
        log.warning("прервана индексация: %s — документы в failed", ", ".join(interrupted))
