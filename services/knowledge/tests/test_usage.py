"""Учёт токенов knowledge (C4): обёртки функций LightRAG пишут llm.usage на вызов."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
from regagent_knowledge import lightrag_factory
from regagent_knowledge.events import CURRENT_JOB
from regagent_knowledge.lightrag_factory import make_embedding_func, make_llm_func
from regagent_knowledge.settings import KnowledgeSettings


def _usage(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [line for line in lines if line["event"] == "llm.usage"]


@pytest.fixture
def settings(knowledge_env: pytest.MonkeyPatch) -> KnowledgeSettings:
    return KnowledgeSettings(_env_file=None)


async def test_extraction_call_reports_provider_usage_and_job(
    settings: KnowledgeSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def complete(model: str, prompt: str, *, token_tracker: Any, **kwargs: Any) -> str:
        token_tracker.add_usage(
            {"prompt_tokens": 900, "completion_tokens": 300, "total_tokens": 1200}
        )
        return "entities"

    monkeypatch.setattr(lightrag_factory, "openai_complete_if_cache", complete)
    token = CURRENT_JOB.set("job-1")
    try:
        assert await make_llm_func(settings)("извлеки сущности из пункта") == "entities"
    finally:
        CURRENT_JOB.reset(token)
    [event] = _usage(capsys)
    assert (event["service"], event["operation"], event["model"]) == (
        "knowledge",
        "index",
        "m",
    )
    assert (event["input_tokens"], event["output_tokens"]) == (900, 300)
    assert (event["usage_source"], event["job_id"]) == ("provider", "job-1")
    assert "сущности" not in json.dumps(event, ensure_ascii=False)


async def test_extraction_without_usage_and_failure(
    settings: KnowledgeSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    async def complete(model: str, prompt: str, *, token_tracker: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError
        return "ok"

    monkeypatch.setattr(lightrag_factory, "openai_complete_if_cache", complete)
    llm = make_llm_func(settings)
    await llm("a")
    with pytest.raises(TimeoutError):
        await llm("b")
    ok, failed = _usage(capsys)
    assert (ok["status"], ok["usage_source"]) == ("ok", "unknown")
    assert (failed["status"], failed["error_code"]) == ("timeout", "TimeoutError")


async def test_embeddings_report_usage_per_call(
    settings: KnowledgeSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def embed(texts: list[str], *, token_tracker: Any, **kwargs: Any) -> np.ndarray:
        token_tracker.add_usage({"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)})
        return np.zeros((len(texts), 4))

    monkeypatch.setattr(lightrag_factory, "_raw_embed", embed)
    func = make_embedding_func(settings)
    await func.func(["a", "b"])
    [event] = _usage(capsys)
    assert (event["operation"], event["model"], event["input_tokens"]) == (
        "embedding",
        "bge-m3",
        14,
    )
    assert event["items"] == 2
