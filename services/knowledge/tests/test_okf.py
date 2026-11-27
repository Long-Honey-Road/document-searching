from datetime import date
from pathlib import Path

import pytest
import yaml
from git import Repo
from regagent_knowledge.manifest import ManifestEntry
from regagent_knowledge.normalize import split_clauses
from regagent_knowledge.okf import OkfStore


@pytest.fixture
def store(tmp_path: Path) -> OkfStore:
    store = OkfStore(tmp_path)
    store.init()
    return store


def entry(version: int) -> ManifestEntry:
    return ManifestEntry(
        file="a.docx",
        doc_id="a",
        title="Регламент",
        areas=["ДИБ"],
        access="public",
        version=version,
        effective_from=date(2026, 9, 1),
        supersedes="a@3" if version == 4 else None,
    )


def test_write_creates_file_and_commit(store: OkfStore, tmp_path: Path) -> None:
    clauses = split_clauses("2. Назначение\n\n2.1. Решение")
    path = store.write_regulation(entry(3), clauses, "hash")
    store.write_regulation(entry(3), clauses, "hash")

    assert path == tmp_path / "docs/a/v3.md"
    _, header, body = path.read_text().split("---", maxsplit=2)
    meta = yaml.safe_load(header)
    assert meta["sources"] == [{"file": "a.docx", "sha256": "hash"}]
    assert meta["status"] == "active"
    assert body.count("2.1. Решение") == 1
    assert len(list(Repo(tmp_path).iter_commits())) == 1


def test_new_version_deprecates_previous(store: OkfStore, tmp_path: Path) -> None:
    clauses = split_clauses("2. Назначение")
    old_path = store.write_regulation(entry(3), clauses, "hash")
    store.write_regulation(entry(4), clauses, "new-hash")

    assert yaml.safe_load(old_path.read_text().split("---", 2)[1])["status"] == "deprecated"
    view = store.read_clause("a", "2")
    assert view is not None
    assert view.entry_meta["version"] == 4
    assert len(list(Repo(tmp_path).iter_commits())) == 2


def test_read_clause_returns_neighbors(store: OkfStore) -> None:
    store.write_regulation(entry(3), split_clauses("1. Первый\n\n2. Второй\n\n3. Третий"), "hash")

    view = store.read_clause("a", "2")

    assert view is not None
    assert view.clause.title == "Второй"
    assert [clause.number for clause in view.neighbors] == ["1", "3"]


def test_read_deleted_returns_none(store: OkfStore) -> None:
    store.write_regulation(entry(3), split_clauses("2. Назначение"), "hash")
    store.set_status("a", 3, "deleted")

    assert store.read_clause("a", "2") is None
    assert store.read_clause("a", "2", version=3) is None
