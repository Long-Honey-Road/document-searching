from pathlib import Path

import yaml
from docx import Document

from scripts.synth.generate import build_corpus


def test_demo_corpus_and_revision_are_manifest_driven(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    for stem in (
        "dbrct-project-activity",
        "dib-presale-activity",
        "dib-project-activity",
        "project-activity-organization",
    ):
        sample = Document()
        sample.add_paragraph(stem)
        sample.save(str(samples_dir / f"{stem}.docx"))
    docs_dir = tmp_path / "docs"

    manifest = build_corpus(samples_dir, docs_dir, revision=1)
    contents = yaml.safe_load(manifest.read_bytes())
    documents = {entry["doc_id"]: entry for entry in contents["documents"]}
    assert len(documents) == 24
    assert "license-return" in documents
    assert "license-key-delivery" in documents
    assert {area["code"] for area in contents["areas"]} == {"ДИБ", "ДБРЦТ", "общий"}
    assert {entry["access"] for entry in documents.values()} == {"public", "closed_fin"}
    assert documents["deal-margin-control"]["access"] == "public"
    assert documents["finance-documents"]["access"] == "closed_fin"
    assert all((docs_dir / entry["file"]).is_file() for entry in documents.values())
    margin_v1 = docs_dir / documents["deal-margin-control"]["file"]
    first_revision = margin_v1.read_bytes()

    build_corpus(samples_dir, docs_dir, revision=2)
    contents = yaml.safe_load(manifest.read_bytes())
    updated = {entry["doc_id"]: entry for entry in contents["documents"]}
    margin = updated.pop("deal-margin-control")
    documents.pop("deal-margin-control")
    assert updated == documents
    assert margin["version"] == 2 and margin["supersedes"] == "deal-margin-control@1"
    assert margin_v1.read_bytes() == first_revision
    assert margin_v1.name != margin["file"]
    revised_doc = Document(docs_dir / margin["file"])
    assert any("1 рабочего дня" in paragraph.text for paragraph in revised_doc.paragraphs)
