"""Манифест папки с документами (dev_docs/03-knowledge-service.md#манифест).

Без записи в манифесте документ не индексируется. Ошибка в одной записи не роняет загрузку:
запись попадает в ``errors`` (на служебной странице — статус ``invalid_manifest``), остальные
загружаются. Ошибка всего файла (нет файла, битый YAML) — одна ошибка без документов.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from regagent_common.access import AccessLevels
from regagent_common.contracts import Area
from regagent_common.fragments import is_header_value

COMMON_AREA = "общий"

DocId = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    doc_id: DocId
    title: Annotated[str, Field(min_length=1)]
    areas: Annotated[list[str], Field(min_length=1)]
    access: str
    version: Annotated[StrictInt, Field(ge=1)]
    effective_from: date
    supersedes: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*@\d+$")] | None = None

    @field_validator("file")
    @classmethod
    def _relative_inside_docs(cls, value: str) -> str:
        p = PurePosixPath(value)
        if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
            raise ValueError(f"путь должен быть относительным внутри папки документов: {value!r}")
        return value


@dataclass(frozen=True)
class ManifestError:
    doc_id: str | None
    file: str | None
    message: str


@dataclass
class ManifestLoadResult:
    areas: list[Area] = field(default_factory=list)
    valid: list[ManifestEntry] = field(default_factory=list)
    errors: list[ManifestError] = field(default_factory=list)

    @property
    def area_codes(self) -> set[str]:
        return {a.code for a in self.areas}


def _str_or_none(raw: Any, key: str) -> str | None:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value if isinstance(value, str) else None


def _load_areas(raw: Any, errors: list[ManifestError]) -> list[Area]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(ManifestError(None, None, "секция areas должна быть списком"))
        return []
    areas: list[Area] = []
    for item in raw:
        try:
            area = Area.model_validate(item)
        except ValidationError as exc:
            errors.append(ManifestError(None, None, f"область {item!r}: {exc.errors()[0]['msg']}"))
            continue
        if not is_header_value(area.code):
            errors.append(
                ManifestError(
                    None, None, f"код области {area.code!r}: без пробелов и символов = ⟦ ⟧"
                )
            )
            continue
        areas.append(area)
    dup = {code for code, n in Counter(a.code for a in areas).items() if n > 1}
    for code in sorted(dup):
        errors.append(ManifestError(None, None, f"код области {code!r} повторяется"))
    return [a for a in areas if a.code not in dup]


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(x) for x in err["loc"])
    return f"{where}: {err['msg']}" if where else str(err["msg"])


def load_manifest(path: Path, levels: AccessLevels) -> ManifestLoadResult:
    result = ManifestLoadResult()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        result.errors.append(ManifestError(None, None, f"{path}: не читается: {exc}"))
        return result
    if not isinstance(data, dict):
        result.errors.append(ManifestError(None, None, f"{path}: ожидается словарь"))
        return result

    result.areas = _load_areas(data.get("areas"), result.errors)
    codes = result.area_codes
    raw_docs = data.get("documents") or []
    if not isinstance(raw_docs, list):
        result.errors.append(ManifestError(None, None, "секция documents должна быть списком"))
        return result

    parsed: list[ManifestEntry] = []
    for raw in raw_docs:
        doc_id, file = _str_or_none(raw, "doc_id"), _str_or_none(raw, "file")
        try:
            entry = ManifestEntry.model_validate(raw)
        except ValidationError as exc:
            result.errors.append(ManifestError(doc_id, file, _first_error(exc)))
            continue
        problems = []
        if not levels.known(entry.access):
            problems.append(f"неизвестный уровень доступа {entry.access!r}")
        unknown = [a for a in entry.areas if a not in codes]
        if unknown:
            problems.append(f"неизвестные области: {', '.join(unknown)}")
        if problems:
            result.errors.append(ManifestError(entry.doc_id, entry.file, "; ".join(problems)))
            continue
        parsed.append(entry)

    dup_ids = {k for k, n in Counter(e.doc_id for e in parsed).items() if n > 1}
    dup_files = {k for k, n in Counter(e.file for e in parsed).items() if n > 1}
    for e in parsed:
        if e.doc_id in dup_ids:
            result.errors.append(ManifestError(e.doc_id, e.file, "doc_id повторяется"))
        elif e.file in dup_files:
            result.errors.append(
                ManifestError(e.doc_id, e.file, "файл указан в нескольких записях")
            )
        else:
            result.valid.append(e)
    return result
