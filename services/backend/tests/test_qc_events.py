"""События ОКК в общем формате D-52 (стык «Логи»): JSON-строка в stdout со всеми полями."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from regagent_backend.qc_store import qc_event


def test_qc_event_is_a_json_line_with_ids(capsys: pytest.CaptureFixture[str]) -> None:
    run = UUID("6f1c2e9a-0000-4000-8000-000000000001")
    qc_event(
        "qc.analytics.write",
        "failed",
        duration_ms=12,
        error_code="analytics_unavailable",
        run_id=run,
    )
    [line] = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert (line["service"], line["event"], line["status"]) == (
        "backend",
        "qc.analytics.write",
        "failed",
    )
    assert (line["duration_ms"], line["error_code"], line["run_id"]) == (
        12,
        "analytics_unavailable",
        str(run),
    )
