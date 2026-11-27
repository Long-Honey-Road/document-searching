"""Публикация и проверка доступности пояснений из OKF."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import monotonic
from typing import Literal, Protocol

from regagent_common.contracts import (
    PracticeBinding,
    PracticePublicationResult,
    PracticePublish,
    PracticeRecheck,
    PracticeSource,
)
from regagent_common.fragments import FragmentHeader, format_header

from .okf import OkfStore, PracticeRecord
from .rag import FRAGMENT_SEPARATOR
from .registry import DocumentRecord

log = logging.getLogger(__name__)


class PracticeRegistry(Protocol):
    async def get(self, doc_id: str) -> DocumentRecord | None: ...


class PracticeIndex(Protocol):
    async def insert(self, level: str, doc_key: str, chunks: list[str]) -> None: ...

    async def delete(self, level: str, doc_key: str) -> None: ...


def _key(command: PracticePublish) -> str:
    return f"practice:{command.practice_id}"


def _chunks(record: PracticeRecord) -> list[str]:
    anchors: dict[str, PracticeBinding] = {}
    for binding in record.command.binds:
        for area in record.binding_areas[binding.doc_id]:
            anchors.setdefault(area, binding)
    return [
        format_header(
            FragmentHeader(
                anchor.doc_id,
                anchor.version,
                anchor.clause,
                area,
                record.level,
                record.command.practice_id,
                record.command.mode,
            )
        )
        + "\n"
        + record.command.text.strip()
        for area, anchor in sorted(anchors.items())
    ]


class PracticeService:
    def __init__(
        self,
        okf: OkfStore,
        registry: PracticeRegistry,
        rag: PracticeIndex,
        *,
        fits_chunk: Callable[[str], bool] | None = None,
    ) -> None:
        self.okf = okf
        self.registry = registry
        self.rag = rag
        self.fits_chunk = fits_chunk

    async def _resolve(self, command: PracticePublish) -> PracticeRecord | None:
        documents = {}
        for binding in command.binds:
            row = await self.registry.get(binding.doc_id)
            if (
                row is None
                or row.status != "indexed"
                or row.version != binding.version
                or row.level not in command.approved_levels
                or not row.sha256
            ):
                return None
            clause = await asyncio.to_thread(
                self.okf.read_clause, binding.doc_id, binding.clause, binding.version
            )
            if clause is None or clause.entry_meta.get("status") != "active":
                return None
            documents[row.doc_id] = row
        levels = {row.level for row in documents.values()}
        if len(levels) != 1:
            return None
        level = next(iter(levels))
        if level is None:
            return None
        return PracticeRecord(
            command,
            "pending",
            level,
            {row.doc_id: sorted(set(row.areas)) for row in documents.values()},
            {row.doc_id: row.sha256 for row in documents.values() if row.sha256},
        )

    async def _current(self, record: PracticeRecord) -> bool:
        for binding in record.command.binds:
            row = await self.registry.get(binding.doc_id)
            if (
                row is None
                or row.status != "indexed"
                or row.version != binding.version
                or row.level != record.level
                or row.sha256 != record.hashes.get(binding.doc_id)
                or sorted(set(row.areas)) != record.binding_areas.get(binding.doc_id)
            ):
                return False
        return True

    async def publish(self, command: PracticePublish) -> PracticePublicationResult:
        started = monotonic()
        error = await self._publish(command)
        status = "failed" if error else "published"
        log.info(
            "event=practice_publication status=%s ticket_id=%s practice_id=%s "
            "revision=%d error_code=%s duration_ms=%d",
            status,
            command.ticket_id,
            command.practice_id,
            command.revision,
            error or "-",
            (monotonic() - started) * 1000,
        )
        return PracticePublicationResult(
            ticket_id=command.ticket_id,
            practice_id=command.practice_id,
            revision=command.revision,
            status=status,
            error_code=error,
        )

    async def _publish(self, command: PracticePublish) -> str | None:
        command = command.model_copy(update={"text": command.text.strip()})
        previous = await asyncio.to_thread(self.okf.read_practice, command.practice_id)
        if previous is not None:
            if previous.command.revision > command.revision:
                return "stale_revision"
            if previous.command.revision == command.revision:
                envelope = {"message_id", "created_at"}
                if previous.command.model_dump(exclude=envelope) != command.model_dump(
                    exclude=envelope
                ):
                    return "revision_conflict"
                if previous.status == "review":
                    return "binding_unavailable"
                if previous.status == "active":
                    return None if await self._current(previous) else "binding_unavailable"
                command = previous.command

        record = await self._resolve(command)
        if record is None:
            return "binding_unavailable"
        chunks = _chunks(record)
        if not chunks:
            return "binding_unavailable"
        if FRAGMENT_SEPARATOR in command.text or (
            self.fits_chunk is not None and any(not self.fits_chunk(chunk) for chunk in chunks)
        ):
            return "invalid_text"

        # При смене workspace убираем старое содержимое, пока его исходный уровень известен.
        if previous is not None and previous.level != record.level:
            await self.rag.delete(previous.level, _key(command))
        await asyncio.to_thread(self.okf.write_practice, record)
        await self.rag.insert(record.level, _key(command), chunks)
        if not await self._current(record):
            await self.rag.delete(record.level, _key(command))
            return "binding_unavailable"
        await asyncio.to_thread(self.okf.write_practice, replace(record, status="active"))
        return None

    async def visible(self, header: FragmentHeader, raw: str) -> PracticeSource | None:
        if header.practice_id is None:
            return None
        record = await asyncio.to_thread(self.okf.read_practice, header.practice_id)
        if (
            record is None
            or record.status != "active"
            or raw.strip() not in _chunks(record)
            or not await self._current(record)
        ):
            return None
        return PracticeSource(mode=record.command.mode, binds=record.command.binds)

    async def _recheck(self, record: PracticeRecord) -> PracticeRecheck | None:
        command = record.command
        for binding in command.binds:
            row = await self.registry.get(binding.doc_id)
            if row is None or row.status == "deleted":
                reason: Literal["new_version", "deleted", "access_changed", "content_changed"] = (
                    "deleted"
                )
            elif row.status != "indexed":
                continue
            elif row.level != record.level:
                reason = "access_changed"
            elif row.version != binding.version:
                reason = "new_version"
            elif row.sha256 != record.hashes.get(binding.doc_id) or sorted(
                set(row.areas)
            ) != record.binding_areas.get(binding.doc_id):
                reason = "content_changed"
            else:
                continue
            return PracticeRecheck(
                message_id=f"recheck:{command.practice_id}:{command.revision}",
                ticket_id=command.ticket_id,
                practice_id=command.practice_id,
                revision=command.revision,
                doc_id=binding.doc_id,
                reason=reason,
            )
        return None

    async def review_changes(self, emit: Callable[[PracticeRecheck], Awaitable[None]]) -> None:
        for record in await asyncio.to_thread(self.okf.practices):
            if record.status == "active":
                event = await self._recheck(record)
                if event is None:
                    continue
                record = replace(record, status="review", recheck=event)
                await asyncio.to_thread(self.okf.write_practice, record)
            if record.recheck is None or record.recheck_sent:
                continue
            started = monotonic()
            await self.rag.delete(record.level, _key(record.command))
            await emit(record.recheck)
            await asyncio.to_thread(self.okf.write_practice, replace(record, recheck_sent=True))
            log.info(
                "event=practice_recheck status=queued ticket_id=%s practice_id=%s "
                "reason=%s duration_ms=%d",
                record.command.ticket_id,
                record.command.practice_id,
                record.recheck.reason,
                (monotonic() - started) * 1000,
            )
