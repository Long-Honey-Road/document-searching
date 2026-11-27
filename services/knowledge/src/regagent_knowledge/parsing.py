"""DOCX → markdown с сохранением нумерации пунктов (Docling).

Номера пунктов в регламентах заказчика набраны текстом («2.1.3.»), а автонумерация Word
стоит на списках. В markdown Docling пункт автонумерованного списка выглядит так же, как
пункт регламента («1. текст»), поэтому каждый элемент списка Word выводится явной строкой
списка: ``- текст`` или ``- 1. текст``. Нормализация считает пунктами только строки, которые
не начинаются с ``- ``.
"""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Protocol

from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter
from docling_core.transforms.serializer.markdown import MarkdownDocSerializer, MarkdownParams
from docling_core.types.doc import ListItem  # type: ignore[attr-defined]  # нет __all__

# Невидимая метка начала элемента списка Word; в итоговом тексте не остаётся.
_LIST_MARK = "⁣LI⁣"
_LIST_LINE = re.compile(rf"^(?P<indent>\s*)(?P<marker>\S+)?\s*{_LIST_MARK}")


class UnsupportedFormatError(ValueError):
    pass


class Parser(Protocol):
    def parse(self, path: Path) -> str: ...


class DoclingParser:
    # TECH-DEBT TD-02: PDF требует моделей разметки Docling, которые на стенде нужно
    # заранее положить в образ; в этапе 1 корпус — DOCX.
    SUPPORTED = frozenset({".docx"})

    @cached_property
    def _converter(self) -> DocumentConverter:
        return DocumentConverter(allowed_formats=[InputFormat.DOCX])

    def parse(self, path: Path) -> str:
        if path.suffix.lower() not in self.SUPPORTED:
            raise UnsupportedFormatError(f"формат {path.suffix!r} пока не поддерживается: {path}")
        doc = self._converter.convert(path).document
        markers: list[str] = []
        for item, _level in doc.iterate_items():
            if isinstance(item, ListItem):
                markers.append(item.marker if item.enumerated else "")
                item.text = _LIST_MARK + item.text
        md = (
            MarkdownDocSerializer(
                doc=doc,
                params=MarkdownParams(escape_underscores=False, escape_html=False),
            )
            .serialize()
            .text
        )
        return _explicit_list_lines(md, markers)


def _explicit_list_lines(md: str, markers: list[str]) -> str:
    out: list[str] = []
    pending = iter(markers)
    for line in md.splitlines():
        m = _LIST_LINE.match(line)
        if m:
            orig = next(pending, "")
            prefix = f"- {orig} " if orig else "- "
            line = m["indent"] + prefix + line[m.end() :].lstrip()
        out.append(line.replace(_LIST_MARK, ""))
    return "\n".join(out).strip() + "\n"
