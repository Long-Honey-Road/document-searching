"""Демо-практика привязана к пунктам действующего синтетического корпуса."""

from pathlib import Path

import yaml
from docx import Document
from regagent_backend.qc_models import PublishDraft

ROOT = Path(__file__).resolve().parents[2]


def test_demo_practice_bindings_exist_in_public_corpus() -> None:
    folder = ROOT / "data/demo_corpus"
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    documents = {row["doc_id"]: row for row in manifest["documents"]}
    practices = yaml.safe_load((ROOT / "data/practice_seeds.yaml").read_text(encoding="utf-8"))[
        "practices"
    ]
    assert {item["key"] for item in practices} == {
        "travel-weekend-taxi",
        "dib-incomplete-project-card",
    }
    areas = set()
    for item in practices:
        draft = PublishDraft.model_validate(item["draft"])
        assert item["question"] and draft.text
        for bind in draft.binds:
            entry = documents[bind.doc_id]
            assert entry["version"] == bind.version
            assert entry["access"] == "public"
            areas.update(entry["areas"])
            paragraphs = [p.text.strip() for p in Document(folder / entry["file"]).paragraphs]
            assert any(p.startswith(f"{bind.clause}. ") for p in paragraphs)
    assert areas == {"общий", "ДИБ"}
