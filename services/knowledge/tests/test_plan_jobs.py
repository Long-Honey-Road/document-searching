from datetime import date

from regagent_knowledge.manifest import ManifestEntry
from regagent_knowledge.registry import DocumentRecord, plan_jobs


def entry(doc_id: str = "a", **over: object) -> ManifestEntry:
    fields: dict[str, object] = {
        "file": f"docs/{doc_id}.docx",
        "doc_id": doc_id,
        "title": "Документ",
        "areas": ["ДИБ"],
        "access": "public",
        "version": 1,
        "effective_from": date(2026, 9, 1),
    }
    fields.update(over)
    return ManifestEntry.model_validate(fields)


def record(doc_id: str = "a", **over: object) -> DocumentRecord:
    fields: dict[str, object] = {
        "doc_id": doc_id,
        "version": 1,
        "sha256": "s1",
        "level": "public",
        "areas": ["ДИБ"],
        "title": "Документ",
        "status": "indexed",
        "error": None,
    }
    fields.update(over)
    return DocumentRecord(**fields)  # type: ignore[arg-type]


def test_new_entry_produces_upsert() -> None:
    jobs = plan_jobs([entry()], {"docs/a.docx": "s1"}, [])
    assert [(j.op, j.doc_id, j.version, j.path, j.sha256) for j in jobs] == [
        ("upsert", "a", 1, "docs/a.docx", "s1")
    ]


def test_same_sha_produces_no_job() -> None:
    assert plan_jobs([entry()], {"docs/a.docx": "s1"}, [record()]) == []


def test_changed_sha_produces_upsert() -> None:
    jobs = plan_jobs([entry()], {"docs/a.docx": "s2"}, [record()])
    assert [(j.op, j.sha256) for j in jobs] == [("upsert", "s2")]


def test_version_bump_produces_upsert() -> None:
    jobs = plan_jobs([entry(version=2)], {"docs/a.docx": "s1"}, [record()])
    assert [(j.op, j.version) for j in jobs] == [("upsert", 2)]


def test_removed_entry_produces_delete() -> None:
    jobs = plan_jobs([], {}, [record(version=3)])
    assert [(j.op, j.doc_id, j.version) for j in jobs] == [("delete", "a", 3)]


def test_missing_file_is_not_job() -> None:
    assert plan_jobs([entry()], {}, []) == []


def test_access_level_change_reindexes() -> None:
    # Иначе документ останется в workspace старого уровня (права!).
    jobs = plan_jobs([entry(access="closed_fin")], {"docs/a.docx": "s1"}, [record()])
    assert [j.op for j in jobs] == ["upsert"]


def test_areas_or_title_change_reindexes() -> None:
    hashes = {"docs/a.docx": "s1"}
    assert plan_jobs([entry(areas=["ДИБ", "общий"])], hashes, [record()])
    assert plan_jobs([entry(title="Новое название")], hashes, [record()])


def test_already_deleted_record_is_not_deleted_again() -> None:
    assert plan_jobs([], {}, [record(status="deleted")]) == []


def test_deleted_record_reappears_as_upsert() -> None:
    jobs = plan_jobs([entry()], {"docs/a.docx": "s1"}, [record(status="deleted")])
    assert [j.op for j in jobs] == ["upsert"]


def test_record_from_invalid_manifest_reappears_as_upsert() -> None:
    rec = record(status="invalid_manifest", sha256=None, version=None, level=None)
    jobs = plan_jobs([entry()], {"docs/a.docx": "s1"}, [rec])
    assert [j.op for j in jobs] == ["upsert"]


def test_failed_with_same_input_is_not_requeued_automatically() -> None:
    # Повтор упавшей задачи — вручную (/admin/jobs/{id}/retry), иначе цикл падений.
    assert plan_jobs([entry()], {"docs/a.docx": "s1"}, [record(status="failed")]) == []


def test_invalid_manifest_record_without_entry_is_not_deleted() -> None:
    # Его нет в индексе — удалять нечего.
    rec = record(status="invalid_manifest", sha256=None, version=None, level=None)
    assert plan_jobs([], {}, [rec]) == []
