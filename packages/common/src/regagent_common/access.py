"""Уровни доступа: уровень → группы Keycloak (config/access_levels.yaml).

Группа ``"*"`` — любой вошедший пользователь. Уровни вычисляются только кодом из групп токена.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

ANY_USER = "*"
# Имя уровня идёт в имя workspace ``lvl_<level>``; ведущее подчёркивание занято служебным
# пустым workspace ``lvl__sentinel``.
_LEVEL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


class AccessLevels:
    def __init__(self, levels: Mapping[str, Iterable[str]]) -> None:
        if not levels:
            raise ValueError("не задано ни одного уровня доступа")
        self._groups: dict[str, frozenset[str]] = {}
        for name, groups in levels.items():
            if not isinstance(name, str) or not _LEVEL_NAME_RE.match(name):
                raise ValueError(f"недопустимое имя уровня доступа: {name!r}")
            group_set = frozenset(groups)
            if not group_set or not all(isinstance(g, str) and g for g in group_set):
                raise ValueError(f"у уровня {name!r} должен быть непустой список групп-строк")
            self._groups[name] = group_set

    @classmethod
    def from_yaml(cls, path: Path) -> AccessLevels:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("levels"), dict):
            raise ValueError(f"{path}: ожидается секция 'levels'")
        levels: dict[str, list[str]] = {}
        for name, spec in data["levels"].items():
            groups = spec.get("groups") if isinstance(spec, dict) else None
            if not isinstance(groups, list):
                raise ValueError(f"{path}: у уровня {name!r} нет списка 'groups'")
            levels[name] = groups
        return cls(levels)

    def levels_for(self, groups: Iterable[str]) -> frozenset[str]:
        user_groups = frozenset(groups)
        return frozenset(
            name
            for name, allowed in self._groups.items()
            if ANY_USER in allowed or allowed & user_groups
        )

    def known(self, level: str) -> bool:
        return level in self._groups

    def all(self) -> list[str]:
        return list(self._groups)
