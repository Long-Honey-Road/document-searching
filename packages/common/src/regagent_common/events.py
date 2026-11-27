"""Структурированные события сервисов: одна JSON-строка на событие в stdout контейнера.

Общие поля (план 28.11, «Логи»): ``ts``, ``service``, ``event``, ``status``, ``duration_ms``,
``error_code`` и поля связи (``run_id``, ``thread_id``, ``job_id``, ``doc_id``, ``ticket_id``,
``practice_id``…). Метки для агрегации — низкокардинальные ``service``, ``event``, ``status``;
идентификаторы остаются полями.

В событие не попадают тексты и секреты: поля с именами вроде ``text``, ``query``, ``prompt``,
``password``, ``token`` отбрасываются, строки длиннее ``MAX_VALUE_CHARS`` или с переводом
строки — тоже. Имена отброшенных полей пишутся в ``dropped_fields``. ``emit`` не бросает
исключений: сбой журнала не должен ломать ответ пользователю.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

LOGGER = "regagent.events"
MAX_VALUE_CHARS = 128
MAX_LIST_ITEMS = 50
_RESERVED = frozenset({"ts", "service", "event", "status", "duration_ms", "error_code"})
# Части имён полей, под которыми обычно лежат тексты, промпты, ответы и секреты
_FORBIDDEN = re.compile(
    r"text|query|question|prompt|answer|message|content|comment|password|secret|token(?!s$)|"
    r"api_key|authorization|cookie",
    re.IGNORECASE,
)
_TOKEN_COUNTS = re.compile(r"^(?:input|output|total|cached)_tokens$")

Scalar = str | int | float | bool | None
Value = Scalar | list[str]


class _StdoutHandler(logging.Handler):
    """Пишет в текущий ``sys.stdout`` (а не в захваченный при создании поток)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(record.getMessage() + "\n")
            sys.stdout.flush()
        except Exception:  # журнал не должен ронять сервис
            self.handleError(record)


def _logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER)
    if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
        logger.addHandler(_StdoutHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _clean(value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, bool | int):
        return True, value
    if isinstance(value, float):
        return True, round(value, 3)
    if isinstance(value, str):
        return len(value) <= MAX_VALUE_CHARS and "\n" not in value and "\r" not in value, value
    if isinstance(value, list | tuple) and all(isinstance(item, str) for item in value):
        ok = all(len(item) <= MAX_VALUE_CHARS and "\n" not in item for item in value)
        return ok, list(value)
    return False, None


def _allowed_name(name: str) -> bool:
    return bool(_TOKEN_COUNTS.match(name)) or not _FORBIDDEN.search(name)


class EventLog:
    """Журнал событий одного сервиса: ``EventLog("backend").emit("guard.classify", ...)``."""

    def __init__(self, service: str) -> None:
        self.service = service

    def emit(
        self,
        event: str,
        *,
        status: str,
        duration_ms: float | None = None,
        error_code: str | None = None,
        **fields: Value,
    ) -> None:
        try:
            self._emit(event, status, duration_ms, error_code, fields)
        except Exception:  # журнал не должен ронять сервис
            logging.getLogger(__name__).exception("event log failed")

    def _emit(
        self,
        event: str,
        status: str,
        duration_ms: float | None,
        error_code: str | None,
        fields: dict[str, Value],
    ) -> None:
        line: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "service": self.service,
            "event": event,
            "status": status,
        }
        if duration_ms is not None:
            line["duration_ms"] = round(duration_ms)
        if error_code is not None:
            line["error_code"] = error_code
        dropped: list[str] = []
        for name, value in fields.items():
            ok, cleaned = _clean(value)
            if name in _RESERVED or not _allowed_name(name) or not ok:
                dropped.append(name)
                continue
            if isinstance(cleaned, list) and len(cleaned) > MAX_LIST_ITEMS:
                line[f"{name}_total"] = len(cleaned)
                cleaned = cleaned[:MAX_LIST_ITEMS]
            line[name] = cleaned
        if dropped:
            line["dropped_fields"] = dropped
        _logger().info(json.dumps(line, ensure_ascii=False, separators=(",", ":")))

    @contextmanager
    def timed(self, event: str, **fields: Value) -> Iterator[dict[str, Value]]:
        """Событие с длительностью блока. В словарь можно дописать поля и ``status``;
        исключение даёт ``status=error`` и ``error_code`` — имя класса, и пробрасывается."""
        extra: dict[str, Value] = dict(fields)
        started = time.perf_counter()
        try:
            yield extra
        except BaseException as exc:
            extra.pop("status", None)
            self.emit(
                event,
                status="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                error_code=type(exc).__name__,
                **extra,
            )
            raise
        status = extra.pop("status", "ok")
        error_code = extra.pop("error_code", None)
        self.emit(
            event,
            status=str(status),
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=str(error_code) if error_code is not None else None,
            **extra,
        )
