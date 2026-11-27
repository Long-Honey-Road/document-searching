"""Парсинг DOCX → markdown. Фикстура собирается в тесте (синтетика, не документ заказчика)
и повторяет особенности образцов: номера пунктов текстом (в том числе жирные), маркированные
и автонумерованные списки Word, таблица."""

from __future__ import annotations

import re
from pathlib import Path

import docx
import pytest
from regagent_knowledge.parsing import DoclingParser, UnsupportedFormatError


@pytest.fixture(scope="module")
def numbered_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = docx.Document()
    d.add_paragraph().add_run("Регламент проверки синтетики").bold = True
    d.add_paragraph().add_run("1. Общие положения").bold = True
    d.add_paragraph("1.1. Документ описывает порядок работы.")
    d.add_paragraph().add_run("2. Назначение ответственного").bold = True
    p = d.add_paragraph()
    p.add_run("2.1.").bold = True
    p.add_run(" Координатор назначает ответственного в течение 1 рабочего дня.")
    d.add_paragraph("автонумерованный шаг первый", style="List Number")
    d.add_paragraph("автонумерованный шаг второй", style="List Number")
    d.add_paragraph("2.1.3. Если данных недостаточно, заявка возвращается.")
    d.add_paragraph("маркированный пункт", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Обязательно", "При наличии"
    t.cell(1, 0).text, t.cell(1, 1).text = "Проект договора", "Матрица ответственности"
    path = tmp_path_factory.mktemp("docx") / "numbered.docx"
    d.save(str(path))
    return path


@pytest.fixture(scope="module")
def markdown(numbered_docx: Path) -> str:
    return DoclingParser().parse(numbered_docx)


def test_numbering_preserved(markdown: str) -> None:
    lines = markdown.splitlines()
    assert any(re.match(r"^\**2\.1\.?\**\s", line) for line in lines)
    assert any(line.startswith("2.1.3.") for line in lines)
    assert any(re.match(r"^\**1\. Общие положения", line) for line in lines)


def test_table_kept_as_markdown_table(markdown: str) -> None:
    rows = [line for line in markdown.splitlines() if line.startswith("|")]
    assert len(rows) >= 3  # заголовок, разделитель, строка
    assert "Обязательно" in rows[0] and "Матрица ответственности" in rows[-1]


def test_word_list_items_are_explicit_list_lines(markdown: str) -> None:
    # Автонумерация Word не должна выглядеть как пункт «1. …» регламента (Review Focus).
    steps = [line for line in markdown.splitlines() if "автонумерованный шаг" in line]
    assert len(steps) == 2
    assert all(line.startswith("- ") for line in steps)
    bullet = [line for line in markdown.splitlines() if "маркированный пункт" in line]
    assert bullet == ["- маркированный пункт"]
    assert not any(re.match(r"^\d+\.\s+автонумерованный", line) for line in markdown.splitlines())


def test_no_service_marks_left(markdown: str) -> None:
    assert "⁣" not in markdown


def test_pdf_is_not_supported_yet(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(UnsupportedFormatError):
        DoclingParser().parse(pdf)
