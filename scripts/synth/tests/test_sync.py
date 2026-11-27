from pathlib import Path

import yaml

from scripts.synth.sync_corpus import sync_corpus


def test_sync_adds_new_documents_without_overwriting_host_edits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "old.docx").write_bytes(b"repository old")
    (source / "new.docx").write_bytes(b"repository new")
    (target / "old.docx").write_bytes(b"host edit")
    old = {"file": "old.docx", "doc_id": "old", "title": "Host", "version": 2}
    new = {"file": "new.docx", "doc_id": "new", "title": "New", "version": 1}
    (source / "manifest.yaml").write_text(yaml.safe_dump({"areas": [], "documents": [old, new]}))
    (target / "manifest.yaml").write_text(yaml.safe_dump({"areas": [], "documents": [old]}))

    assert sync_corpus(source, target) == 1
    assert (target / "old.docx").read_bytes() == b"host edit"
    assert (target / "new.docx").read_bytes() == b"repository new"
    entries = yaml.safe_load((target / "manifest.yaml").read_text())["documents"]
    assert [entry["doc_id"] for entry in entries] == ["old", "new"]
    assert sync_corpus(source, target) == 0


def test_sync_rejects_filename_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "new.docx").write_bytes(b"repository")
    (target / "new.docx").write_bytes(b"host")
    new = {"file": "new.docx", "doc_id": "new"}
    (source / "manifest.yaml").write_text(yaml.safe_dump({"areas": [], "documents": [new]}))
    (target / "manifest.yaml").write_text(yaml.safe_dump({"areas": [], "documents": []}))

    import pytest

    with pytest.raises(ValueError, match=r"new\.docx"):
        sync_corpus(source, target)
    assert yaml.safe_load((target / "manifest.yaml").read_text())["documents"] == []
