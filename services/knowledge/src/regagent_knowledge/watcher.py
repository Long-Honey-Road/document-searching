"""Сверка документов с манифестом и наблюдение за папкой источника."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from regagent_common.access import AccessLevels
from regagent_common.contracts import IndexJob
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .manifest import ManifestEntry, load_manifest
from .registry import Registry, plan_jobs

log = logging.getLogger(__name__)


class IndexPublisher(Protocol):
    async def publish(self, job: IndexJob) -> None: ...


@dataclass(frozen=True)
class SourceManifest:
    entries: dict[str, ManifestEntry]
    invalid: dict[str, str]
    unidentified_error: str | None

    def error_for(self, doc_id: str) -> str | None:
        if doc_id in self.entries:
            return None
        return self.invalid.get(doc_id, self.unidentified_error)


class FolderSource:
    def __init__(self, root: Path, levels: AccessLevels) -> None:
        self.root = root.resolve()
        self.levels = levels

    def path(self, filename: str) -> Path:
        path = (self.root / filename).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"файл находится вне папки документов: {filename}")
        return path

    def manifest(self) -> SourceManifest:
        try:
            loaded = load_manifest(self.path("manifest.yaml"), self.levels)
        except (ValueError, RuntimeError) as exc:
            return SourceManifest({}, {}, str(exc))
        invalid = {
            error.doc_id: error.message for error in loaded.errors if error.doc_id is not None
        }
        invalid_paths = {error.file: error.message for error in loaded.errors if error.file}
        global_error = next((error.message for error in loaded.errors if not error.doc_id), None)
        entries = {}
        for entry in loaded.valid:
            if entry.doc_id in invalid:
                continue
            if error := invalid_paths.get(entry.file):
                invalid[entry.doc_id] = error
                continue
            try:
                self.path(entry.file)
            except (OSError, ValueError, RuntimeError) as exc:
                invalid[entry.doc_id] = str(exc)
                continue
            entries[entry.doc_id] = entry
        return SourceManifest(entries, invalid, global_error)

    def snapshot(self, relative_path: str) -> bytes | None:
        try:
            return self.path(relative_path).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return None

    def scan(self) -> tuple[SourceManifest, dict[str, str]]:
        manifest = self.manifest()
        hashes = {}
        for entry in manifest.entries.values():
            path = self.path(entry.file)
            try:
                if path.is_file():
                    with path.open("rb") as doc:
                        hashes[entry.file] = hashlib.file_digest(doc, "sha256").hexdigest()
                    continue
            except FileNotFoundError:
                pass
            log.warning("файл из манифеста отсутствует: %s", entry.file)
        return manifest, hashes


async def scan(docs_dir: Path, levels: AccessLevels, registry: Registry) -> list[IndexJob]:
    source = FolderSource(docs_dir, levels)
    manifest, hashes = await asyncio.to_thread(source.scan)
    records = {record.doc_id: record for record in await registry.all()}
    for doc_id in records.keys() | manifest.invalid.keys():
        if not (error := manifest.error_for(doc_id)):
            continue
        record = records.get(doc_id)
        if record is None:
            await registry.set_status(doc_id, None, None, None, "invalid_manifest", error)
        elif record.status != "deleted":
            await registry.transition_status(record, "invalid_manifest", error)

    jobs = plan_jobs(
        manifest.entries.values(),
        hashes,
        (record for record in records.values() if manifest.error_for(record.doc_id) is None),
    )
    deleted = {job.doc_id for job in jobs if job.op == "delete"}
    for doc_id, record in records.items():
        if record.status != "indexed":
            continue
        candidate = manifest.entries.get(doc_id)
        if doc_id in deleted or (candidate is not None and candidate.access != record.level):
            await registry.transition_status(record, "indexing")
    return jobs


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, dirty: asyncio.Event) -> None:
        self._loop = loop
        self._dirty = dirty

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"created", "deleted", "moved"} or (
            event.event_type in {"modified", "closed"} and not event.is_directory
        ):
            self._loop.call_soon_threadsafe(self._dirty.set)


def _start_observer(path: Path, handler: _ChangeHandler) -> BaseObserver:
    watch_path = str(path.resolve())
    observer: BaseObserver = Observer()
    observer.schedule(handler, watch_path, recursive=True)
    try:
        observer.start()
    except SystemError:
        # FSEvents может отказаться запускаться в окружении с ограниченными правами.
        log.warning("системное наблюдение недоступно, включён опрос папки документов")
        observer = PollingObserver()
        observer.schedule(handler, watch_path, recursive=True)
        observer.start()
    return observer


async def run_watcher(
    docs_dir: Path,
    levels: AccessLevels,
    registry: Registry,
    publisher: IndexPublisher,
    *,
    debounce_s: float,
    ready: asyncio.Event | None = None,
) -> None:
    dirty = asyncio.Event()
    observer = _start_observer(docs_dir, _ChangeHandler(asyncio.get_running_loop(), dirty))

    async def reconcile() -> None:
        for job in await scan(docs_dir, levels, registry):
            await publisher.publish(job)

    try:
        await reconcile()
        if ready is not None:
            ready.set()
        while True:
            await dirty.wait()
            dirty.clear()
            while debounce_s > 0:
                try:
                    await asyncio.wait_for(dirty.wait(), debounce_s)
                except TimeoutError:
                    break
                dirty.clear()
            try:
                await reconcile()
            except Exception:
                log.exception("не удалось сверить папку документов")
                await asyncio.sleep(max(1.0, debounce_s))
                dirty.set()
    finally:
        observer.stop()
        await asyncio.to_thread(observer.join)
