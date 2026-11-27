from datetime import date

import pytest
from lightrag.utils import Tokenizer
from regagent_common.fragments import parse_header
from regagent_knowledge.manifest import ManifestEntry
from regagent_knowledge.normalize import build_chunks, split_clauses


@pytest.fixture
def entry() -> ManifestEntry:
    return ManifestEntry(
        file="a.docx",
        doc_id="a",
        title="Регламент",
        areas=["ДИБ", "общий"],
        access="closed_fin",
        version=3,
        effective_from=date(2026, 9, 1),
    )


def test_top_level_clause_includes_subclauses() -> None:
    clauses = split_clauses("## 2. Назначение РП\n\n2.1. Решение\n\n2.1.3. Срок\n\n3. Иное")

    assert [clause.number for clause in clauses] == ["2", "2.1", "2.1.3", "3"]
    assert "2.1.3. Срок" in clauses[0].text
    assert "3. Иное" not in clauses[0].text
    assert clauses[2].depth == 3


def test_long_clause_split_by_subclauses(entry: ManifestEntry, tokenizer: Tokenizer) -> None:
    markdown = "2. Назначение\n\n2.1. " + "решение " * 35 + "\n\n2.2. " + "срок " * 29
    chunks = build_chunks(entry, split_clauses(markdown), max_tokens=420, tokenizer=tokenizer)

    assert [chunk.header.clause for chunk in chunks] == ["2.1", "2.2"]


def test_chunk_text_starts_with_header(entry: ManifestEntry, tokenizer: Tokenizer) -> None:
    clauses = split_clauses("2.1. Решение\n\nТекст")
    chunk = build_chunks(entry, clauses, max_tokens=1200, tokenizer=tokenizer)[0]

    assert parse_header(chunk.text) == chunk.header
    assert chunk.header.area == "ДИБ"
    assert chunk.header.level == "closed_fin"
    assert chunk.header.version == 3


def test_unnumbered_document_falls_back_to_headings(
    entry: ManifestEntry, tokenizer: Tokenizer
) -> None:
    clauses = split_clauses("# Назначение\n\nПервый текст\n\n## Срок\n\nВторой текст")
    chunks = build_chunks(entry, clauses, max_tokens=1200, tokenizer=tokenizer)

    assert [chunk.header.clause for chunk in chunks] == ["h1", "h2"]
    assert "Первый текст" in chunks[0].text
    assert "Второй текст" in chunks[1].text


def test_word_list_items_remain_in_clause() -> None:
    clauses = split_clauses("2. Назначение\n\n- 1. Список\n\n- 2. Продолжение")

    assert [clause.number for clause in clauses] == ["2"]
    assert "- 2. Продолжение" in clauses[0].text


def test_docling_bold_numbering_is_retained() -> None:
    clauses = split_clauses("**1. Назначение**\n\n**1.1.** Текст")

    assert [clause.number for clause in clauses] == ["1", "1.1"]
    assert clauses[1].title == "Текст"
