"""Служебный заголовок фрагмента и fragment_id.

Каждый фрагмент, который knowledge кладёт в LightRAG, начинается заголовком, по которому
поиск восстанавливает метаданные и проверяет права (docs/LLD.md, «Общий пакет»).

Регламент: ``⟦doc=<doc_id> v=<version> clause=<clause> area=<area> level=<level>⟧``
Практика:  ``⟦practice=<id> doc=<doc_id> [v=<version>] clause=<clause> mode=<mode> area=<area>
level=<level>⟧`` — ``v=`` необязателен (TECH-DEBT TD-01 в docs/TECH-DEBT.md).

Формат и порядок полей строгие: фрагмент, заголовок которого не разбирается, считается
недоверенным и выбрасывается из выдачи.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Mode = Literal["clarifies", "overrides"]
MODES: frozenset[str] = frozenset({"clarifies", "overrides"})

_VALUE = r"[^\s=⟦⟧]+"
_VALUE_RE = re.compile(rf"{_VALUE}\Z")

_REGULATION_RE = re.compile(
    rf"\s*⟦doc=(?P<doc>{_VALUE}) v=(?P<v>\d+) clause=(?P<clause>{_VALUE}) "
    rf"area=(?P<area>{_VALUE}) level=(?P<level>{_VALUE})⟧"
)
_PRACTICE_RE = re.compile(
    rf"\s*⟦practice=(?P<practice>{_VALUE}) doc=(?P<doc>{_VALUE})(?: v=(?P<v>\d+))? "
    rf"clause=(?P<clause>{_VALUE}) mode=(?P<mode>{_VALUE}) "
    rf"area=(?P<area>{_VALUE}) level=(?P<level>{_VALUE})⟧"
)


@dataclass(frozen=True)
class FragmentHeader:
    doc_id: str
    version: int | None
    clause: str
    area: str
    level: str
    practice_id: str | None = None
    mode: Mode | None = None

    @property
    def is_practice(self) -> bool:
        return self.practice_id is not None


def is_header_value(value: str) -> bool:
    """Можно ли записать значение в заголовок фрагмента (без пробелов, ``=``, ``⟦``, ``⟧``)."""
    return bool(_VALUE_RE.match(value))


def _check(name: str, value: str) -> str:
    if not is_header_value(value):
        raise ValueError(f"недопустимое значение {name}={value!r} для заголовка фрагмента")
    return value


def format_header(h: FragmentHeader) -> str:
    doc = _check("doc", h.doc_id)
    clause = _check("clause", h.clause)
    area = _check("area", h.area)
    level = _check("level", h.level)
    if h.practice_id is None:
        if h.version is None:
            raise ValueError("у фрагмента регламента должна быть версия")
        return f"⟦doc={doc} v={h.version} clause={clause} area={area} level={level}⟧"
    practice = _check("practice", h.practice_id)
    if h.mode not in MODES:
        raise ValueError(f"у фрагмента практики mode должен быть одним из {sorted(MODES)}")
    v = "" if h.version is None else f" v={h.version}"
    return (
        f"⟦practice={practice} doc={doc}{v} clause={clause} mode={h.mode} "
        f"area={area} level={level}⟧"
    )


def parse_header(text: str) -> FragmentHeader | None:
    """Разбирает заголовок в начале текста; ``None`` — заголовка нет или он испорчен."""
    m = _REGULATION_RE.match(text)
    if m:
        return FragmentHeader(
            doc_id=m["doc"],
            version=int(m["v"]),
            clause=m["clause"],
            area=m["area"],
            level=m["level"],
        )
    m = _PRACTICE_RE.match(text)
    if m and m["mode"] in MODES:
        mode: Mode = "clarifies" if m["mode"] == "clarifies" else "overrides"
        return FragmentHeader(
            doc_id=m["doc"],
            version=int(m["v"]) if m["v"] is not None else None,
            clause=m["clause"],
            area=m["area"],
            level=m["level"],
            practice_id=m["practice"],
            mode=mode,
        )
    return None


def fragment_id(h: FragmentHeader) -> str:
    """``<doc_id>@<version>#<clause>`` для регламента, ``practice:<id>`` для практики."""
    if h.practice_id is not None:
        return f"practice:{h.practice_id}"
    if h.version is None:
        raise ValueError("у фрагмента регламента должна быть версия")
    return f"{h.doc_id}@{h.version}#{h.clause}"
