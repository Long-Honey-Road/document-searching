"""Структурированные события: одна JSON-строка в stdout, без текстов и секретов."""

from __future__ import annotations

import json
import logging
from typing import cast

import pytest
from regagent_common.events import EventLog, Value


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_event_is_one_json_line_with_common_fields(capsys: pytest.CaptureFixture[str]) -> None:
    EventLog("backend").emit(
        "guard.classify", status="ok", duration_ms=412.37, run_id="r-1", model="qwen"
    )
    [event] = _lines(capsys)
    assert event["service"] == "backend"
    assert event["event"] == "guard.classify"
    assert event["status"] == "ok"
    assert event["duration_ms"] == 412
    assert event["run_id"] == "r-1"
    assert event["model"] == "qwen"
    assert "error_code" not in event
    assert isinstance(event["ts"], str) and event["ts"].endswith("Z")


def test_error_code_and_counts(capsys: pytest.CaptureFixture[str]) -> None:
    EventLog("knowledge").emit(
        "search.request", status="error", error_code="RagError", fragments=0, levels=2
    )
    [event] = _lines(capsys)
    assert (event["error_code"], event["fragments"], event["levels"]) == ("RagError", 0, 2)


@pytest.mark.parametrize(
    "field",
    ["text", "query", "question", "prompt", "answer", "password", "api_token", "content"],
)
def test_text_and_secret_fields_never_reach_the_log(
    capsys: pytest.CaptureFixture[str], field: str
) -> None:
    fields: dict[str, Value] = {field: "секрет или текст вопроса"}
    EventLog("backend").emit("x", status="ok", **fields)  # type: ignore[arg-type]
    [event] = _lines(capsys)
    assert field not in event
    assert event["dropped_fields"] == [field]
    assert "секрет" not in json.dumps(event, ensure_ascii=False)


def test_long_or_multiline_values_are_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    EventLog("backend").emit(
        "x", status="ok", doc_id="a" * 200, area="ДИБ\nвторая строка", run_id="ok"
    )
    [event] = _lines(capsys)
    assert event["run_id"] == "ok"
    assert set(cast(list[str], event["dropped_fields"])) == {"area", "doc_id"}


def test_lists_of_short_ids_are_kept_and_capped(capsys: pytest.CaptureFixture[str]) -> None:
    ids = [f"doc@1#{i}" for i in range(60)]
    EventLog("backend").emit("x", status="ok", fragment_ids=ids)
    [event] = _lines(capsys)
    assert event["fragment_ids"] == ids[:50]
    assert event["fragment_ids_total"] == 60


def test_emit_never_raises(capsys: pytest.CaptureFixture[str]) -> None:
    EventLog("backend").emit("x", status="ok", run_id=object())  # type: ignore[arg-type]
    [event] = _lines(capsys)
    assert event["dropped_fields"] == ["run_id"]


async def test_timed_records_duration_and_exception(capsys: pytest.CaptureFixture[str]) -> None:
    events = EventLog("knowledge")
    with events.timed("refs.request", doc_id="a") as fields:
        fields["outgoing"] = 2
    with pytest.raises(TimeoutError), events.timed("refs.request", doc_id="b"):
        raise TimeoutError
    ok, failed = _lines(capsys)
    assert (ok["status"], ok["outgoing"], ok["doc_id"]) == ("ok", 2, "a")
    assert isinstance(ok["duration_ms"], int)
    assert (failed["status"], failed["error_code"]) == ("error", "TimeoutError")


def test_timed_status_can_be_set_by_caller(capsys: pytest.CaptureFixture[str]) -> None:
    with EventLog("backend").timed("refs.tool") as fields:
        fields["status"] = "empty"
    [event] = _lines(capsys)
    assert event["status"] == "empty"


def test_events_do_not_reach_the_root_logger(capsys: pytest.CaptureFixture[str]) -> None:
    """Иначе basicConfig сервиса продублирует событие строкой с префиксом уровня."""
    seen: list[logging.LogRecord] = []

    class Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    root = logging.getLogger()
    spy = Spy()
    root.addHandler(spy)
    try:
        EventLog("backend").emit("x", status="ok")
    finally:
        root.removeHandler(spy)
    assert not seen
    assert len(_lines(capsys)) == 1
