"""Стык агента 3 и knowledge: мини-корпус из scripts/synth принимается загрузчиком манифеста
и парсером knowledge (без этого демо-стенд не проиндексирует ни одного документа)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from regagent_common.access import AccessLevels
from regagent_knowledge.manifest import load_manifest
from regagent_knowledge.parsing import DoclingParser

from scripts.synth.generate import build_corpus

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    docs = tmp_path_factory.mktemp("docs")
    build_corpus(ROOT / "data" / "samples", docs)
    return docs


def test_generated_manifest_is_valid_for_knowledge(corpus: Path) -> None:
    levels = AccessLevels.from_yaml(ROOT / "config" / "access_levels.yaml")
    res = load_manifest(corpus / "manifest.yaml", levels)
    assert res.errors == []
    assert {a.code for a in res.areas} == {"ДИБ", "ДБРЦТ", "общий"}
    assert {e.access for e in res.valid} == {"public", "closed_fin"}
    for e in res.valid:
        assert (corpus / e.file).is_file(), e.file


def test_generated_documents_keep_text_numbering(corpus: Path) -> None:
    parser = DoclingParser()
    for path in sorted(corpus.glob("*.docx")):
        md = parser.parse(path)
        assert re.search(r"^\**\d+\.\d+\.", md, re.M), f"нет нумерованных пунктов: {path.name}"
