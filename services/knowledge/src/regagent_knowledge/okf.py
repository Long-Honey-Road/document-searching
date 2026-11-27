"""Редакции регламентов в локальном git-репозитории OKF."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Literal

import yaml
from git import Repo

from .manifest import ManifestEntry
from .normalize import Clause, render_markdown, split_clauses
from .references import DocumentReferences

RegulationStatus = Literal["active", "deprecated", "deleted"]
_DOC_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_REVISION = re.compile(r"v([1-9]\d*)\.md\Z")


@dataclass(frozen=True)
class ClauseView:
    entry_meta: dict[str, Any]
    clause: Clause
    neighbors: list[Clause]


@dataclass(frozen=True)
class RegulationView:
    entry_meta: dict[str, Any]
    clauses: list[Clause]


@dataclass(frozen=True)
class _Revision:
    path: Path
    meta: dict[str, Any]
    body: str

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = yaml.safe_dump(self.meta, allow_unicode=True, sort_keys=False)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as staged:
            try:
                staged.write(f"---\n{header}---\n\n{self.body.rstrip()}\n")
                staged.close()
                os.replace(staged.name, self.path)
            finally:
                Path(staged.name).unlink(missing_ok=True)


def _load_revision(path: Path) -> _Revision:
    start, separator, body = path.read_text(encoding="utf-8").partition("\n---\n")
    if not separator or not start.startswith("---\n"):
        raise ValueError(f"редакция OKF без YAML-шапки: {path}")
    meta = yaml.safe_load(start[4:])
    if not isinstance(meta, dict):
        raise ValueError(f"некорректная YAML-шапка OKF: {path}")
    return _Revision(path, meta, body.strip())


class OkfStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._repo: Repo | None = None
        self._lock = RLock()

    def init(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._repo = Repo.init(self.root)

    def _commit(self, paths: list[Path], message: str) -> None:
        repo = self._repo
        if repo is None:
            raise RuntimeError("репозиторий OKF не инициализирован")
        relative = [str(path.relative_to(self.root)) for path in paths]
        if not relative or not repo.git.status("--porcelain", "--", *relative):
            return
        repo.index.add(relative)
        with repo.git.custom_environment(
            GIT_AUTHOR_NAME="RegAgent Knowledge",
            GIT_AUTHOR_EMAIL="knowledge@localhost",
            GIT_COMMITTER_NAME="RegAgent Knowledge",
            GIT_COMMITTER_EMAIL="knowledge@localhost",
        ):
            repo.git.commit("-m", message, "--only", "--", *relative)

    def _revisions(self, doc_id: str) -> list[_Revision]:
        if not _DOC_ID.fullmatch(doc_id):
            return []
        directory = self.root / "docs" / doc_id
        paths = [
            (int(match[1]), path)
            for path in directory.glob("v*.md")
            if (match := _REVISION.fullmatch(path.name))
        ]
        return [
            row
            for version, path in sorted(paths, reverse=True)
            if (row := _load_revision(path)).meta.get("doc_id") == doc_id
            and row.meta.get("version") == version
        ]

    def _revision(self, doc_id: str, version: int | None) -> _Revision | None:
        if version is None:
            return next(
                (row for row in self._revisions(doc_id) if row.meta.get("status") == "active"),
                None,
            )
        if not _DOC_ID.fullmatch(doc_id) or version < 1:
            return None
        path = self.root / "docs" / doc_id / f"v{version}.md"
        if not path.is_file():
            return None
        revision = _load_revision(path)
        if (
            revision.meta.get("doc_id") == doc_id
            and revision.meta.get("version") == version
            and revision.meta.get("status") in {"active", "deprecated"}
        ):
            return revision
        return None

    def write_regulation(
        self,
        entry: ManifestEntry,
        clauses: list[Clause],
        sha256: str,
        *,
        references: DocumentReferences | None = None,
    ) -> Path:
        path = self.root / "docs" / entry.doc_id / f"v{entry.version}.md"
        body = render_markdown(clauses)
        refs = references or DocumentReferences()
        meta = {
            "type": "regulation",
            "title": entry.title,
            "doc_id": entry.doc_id,
            "version": entry.version,
            "status": "active",
            "areas": entry.areas,
            "access": entry.access,
            "effective_from": entry.effective_from,
            "sources": [{"file": entry.file, "sha256": sha256}],
            "links": [asdict(link) for link in refs.links],
            "links_needing_review": [asdict(link) for link in refs.needs_review],
            "missing_links": [asdict(link) for link in refs.missing],
        }
        if entry.supersedes is not None:
            meta["supersedes"] = entry.supersedes
        with self._lock:
            previous = self._revisions(entry.doc_id)
            existing = next((row for row in previous if row.path == path), None)
            if (
                existing is None
                or existing.body != body
                or {key: value for key, value in existing.meta.items() if key != "generated"}
                != meta
            ):
                meta["generated"] = {"by": "knowledge", "at": datetime.now(UTC)}
                _Revision(path, meta, body).write()
            for row in previous:
                if row.path != path and row.meta.get("status") == "active":
                    row.meta["status"] = "deprecated"
                    row.write()
            paths = [path, *(row.path for row in previous if row.path != path)]
            self._commit(paths, f"{entry.doc_id}@v{entry.version}: active")
        return path

    def set_status(self, doc_id: str, version: int, status: RegulationStatus) -> None:
        if not _DOC_ID.fullmatch(doc_id) or version < 1:
            raise ValueError("некорректная редакция OKF")
        with self._lock:
            path = self.root / "docs" / doc_id / f"v{version}.md"
            if not path.is_file():
                return
            row = _load_revision(path)
            if row.meta.get("status") != status:
                row.meta["status"] = status
                row.write()
            self._commit([path], f"{doc_id}@v{version}: {status}")

    def active_metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            docs = self.root / "docs"
            if not docs.exists():
                return []
            return [
                revision.meta
                for path in sorted(docs.iterdir())
                if (revision := self._revision(path.name, None)) is not None
            ]

    def read_regulation(self, doc_id: str, version: int | None = None) -> RegulationView | None:
        with self._lock:
            revision = self._revision(doc_id, version)
            if revision is None:
                return None
            return RegulationView(revision.meta, split_clauses(revision.body))

    def read_clause(
        self, doc_id: str, clause: str, version: int | None = None
    ) -> ClauseView | None:
        regulation = self.read_regulation(doc_id, version)
        if regulation is None:
            return None
        clauses = regulation.clauses
        for index, part in enumerate(clauses):
            if part.number == clause:
                neighbors = clauses[max(0, index - 1) : index] + clauses[index + 1 : index + 2]
                return ClauseView(regulation.entry_meta, part, neighbors)
        return None
