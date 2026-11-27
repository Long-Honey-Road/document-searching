"""События backend (D-52): общий журнал и поля связи хода."""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.runnables import RunnableConfig
from regagent_common.events import EventLog

events = EventLog("backend")

# Aegra закрепляет эти ключи на сервере: клиент их не подменит
_RUN_KEYS = ("run_id", "thread_id")


def run_fields(config: RunnableConfig | Mapping[str, object] | None) -> dict[str, str]:
    """``run_id`` и ``thread_id`` запуска для событий и конфигов вложенных вызовов."""
    configurable = (config or {}).get("configurable") or {}
    if not isinstance(configurable, Mapping):
        return {}
    return {key: str(configurable[key]) for key in _RUN_KEYS if configurable.get(key)}
