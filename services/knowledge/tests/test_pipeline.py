import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml
from lightrag.utils import Tokenizer
from regagent_common.access import AccessLevels
from regagent_common.contracts import IndexJob
from regagent_knowledge.okf import OkfStore
from regagent_knowledge.pipeline import PipelineDeps, process_job
from regagent_knowledge.registry import DocStatus, DocumentRecord, Registry
from regagent_knowledge.settings import KnowledgeSettings
from regagent_knowledge.watcher import run_watcher, scan

LEVELS = AccessLevels({"public": ["*"], "closed_fin": ["/fin"]})


class MemoryRegistry(Registry):
    def __init__(self) -> None:
        self.docs: dict[str, DocumentRecord] = {}

    async def get(self, doc_id: str) -> DocumentRecord | None:
        return self.docs.get(doc_id)

    async def all(self) -> list[DocumentRecord]:
        return list(self.docs.values())

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
        previous = self.docs.get(doc_id)
        self.docs[doc_id] = DocumentRecord(
            doc_id,
            version,
            sha256,
            level,
            areas if areas is not None else (previous.areas if previous else []),
            title if title is not None else (previous.title if previous else None),
            cast(DocStatus, status),
            error,
        )

    async def transition_status(
        self, record: DocumentRecord, status: DocStatus, error: str | None = None
    ) -> bool:
        if self.docs.get(record.doc_id) != record:
            return False
        await self.set_status(
            record.doc_id, record.version, record.sha256, record.level, status, error
        )
        return True


class Parser:
    def parse(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        if not text:
            raise ValueError("файл пуст")
        return text


class Rag:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.docs: dict[tuple[str, str], str] = {}

    async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None:
        self.calls.append(("insert", level, doc_key))
        self.docs[level, doc_key] = "\n".join(chunks)

    async def delete(self, level: str, doc_key: str) -> None:
        self.calls.append(("delete", level, doc_key))
        self.docs.pop((level, doc_key), None)


@dataclass
class Folder:
    path: Path

    def manifest(self, version: int | None, access: str = "public") -> None:
        entries = (
            [
                {
                    "file": "document.docx",
                    "doc_id": "a",
                    "title": "Регламент",
                    "areas": ["общий"],
                    "access": access,
                    "version": version,
                    "effective_from": "2026-09-01",
                }
            ]
            if version is not None
            else []
        )
        (self.path / "manifest.yaml").write_text(
            yaml.safe_dump(
                {"areas": [{"code": "общий", "title": "Общий"}], "documents": entries},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    def document(self, text: str) -> None:
        (self.path / "document.docx").write_text(text, encoding="utf-8")


@pytest.fixture
def environment(
    tmp_path: Path, knowledge_env: pytest.MonkeyPatch, tokenizer: Tokenizer
) -> tuple[Folder, PipelineDeps, Rag]:
    folder = Folder(tmp_path / "docs")
    folder.path.mkdir()
    folder.manifest(3)
    folder.document("1. Первый")
    store = OkfStore(tmp_path / "okf")
    store.init()
    rag = Rag()
    settings = KnowledgeSettings(docs_dir=folder.path)
    return (
        folder,
        PipelineDeps(
            Parser(), store, rag, MemoryRegistry(), settings, levels=LEVELS, tokenizer=tokenizer
        ),
        rag,
    )


async def test_upsert_deletes_previous_version_then_inserts(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, rag = environment
    (first,) = await scan(folder.path, LEVELS, deps.registry)
    await process_job(first, deps)
    folder.manifest(4, access="closed_fin")
    folder.document("1. Обновлённый")
    (second,) = await scan(folder.path, LEVELS, deps.registry)
    record = await deps.registry.get("a")
    assert record is not None and record.status == "indexing"

    await process_job(second, deps)

    assert rag.calls == [
        ("insert", "public", "a@v3"),
        ("delete", "public", "a@v3"),
        ("insert", "closed_fin", "a@v4"),
    ]
    assert not await scan(folder.path, LEVELS, deps.registry)


async def test_upsert_sets_indexed_status(environment: tuple[Folder, PipelineDeps, Rag]) -> None:
    folder, deps, rag = environment
    (job,) = await scan(folder.path, LEVELS, deps.registry)

    await process_job(job, deps)

    record = await deps.registry.get("a")
    assert record is not None and record.status == "indexed"
    assert rag.docs["public", "a@v3"].startswith("⟦doc=a v=3 clause=1 area=общий level=public⟧")


async def test_failure_sets_failed_status_with_error(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, _ = environment
    folder.document("")
    (job,) = await scan(folder.path, LEVELS, deps.registry)

    with pytest.raises(ValueError, match="файл пуст"):
        await process_job(job, deps)

    record = await deps.registry.get("a")
    assert record is not None
    assert record.status == "failed"
    assert "файл пуст" in (record.error or "")
    assert not await scan(folder.path, LEVELS, deps.registry)


async def test_delete_marks_okf_deleted_and_removes_from_rag(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, rag = environment
    (job,) = await scan(folder.path, LEVELS, deps.registry)
    await process_job(job, deps)
    folder.manifest(None)
    (delete,) = await scan(folder.path, LEVELS, deps.registry)

    await process_job(delete, deps)

    assert ("public", "a@v3") not in rag.docs
    assert deps.okf.read_clause("a", "1", 3) is None
    record = await deps.registry.get("a")
    assert record is not None and record.status == "deleted"


async def test_reprocessing_same_job_is_idempotent(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, rag = environment
    (job,) = await scan(folder.path, LEVELS, deps.registry)

    await process_job(job, deps)
    await process_job(job, deps)

    assert rag.calls == [("insert", "public", "a@v3")]


async def test_queued_upsert_checks_current_file_hash(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, rag = environment
    (job,) = await scan(folder.path, LEVELS, deps.registry)
    folder.document("1. Другой текст")

    await process_job(job, deps)

    assert not rag.docs
    assert (await scan(folder.path, LEVELS, deps.registry))[0].sha256 != job.sha256


@pytest.mark.parametrize("malformed", ["documents: [", "documents: false"])
async def test_malformed_manifest_revokes_existing_document(
    environment: tuple[Folder, PipelineDeps, Rag], malformed: str
) -> None:
    folder, deps, _ = environment
    (job,) = await scan(folder.path, LEVELS, deps.registry)
    await process_job(job, deps)
    (folder.path / "manifest.yaml").write_text(malformed, encoding="utf-8")

    assert not await scan(folder.path, LEVELS, deps.registry)

    record = await deps.registry.get("a")
    assert record is not None and record.status == "invalid_manifest"


async def test_watcher_publishes_initial_and_changed_document(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, _ = environment
    jobs: asyncio.Queue[IndexJob] = asyncio.Queue()
    ready = asyncio.Event()

    class Publisher:
        async def publish(self, job: IndexJob) -> None:
            await jobs.put(job)

    watcher = asyncio.create_task(
        run_watcher(folder.path, LEVELS, deps.registry, Publisher(), debounce_s=0.05, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        initial = jobs.get_nowait()
        assert initial.version == 3
        await process_job(initial, deps)

        folder.document("1. Новая редакция")
        folder.manifest(4)
        changed = await asyncio.wait_for(jobs.get(), timeout=5)

        assert changed.version == 4
        assert changed.sha256 != initial.sha256
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
