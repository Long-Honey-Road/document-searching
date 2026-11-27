"""Учёт токенов (C4): одно событие llm.usage на физический вызов модели."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from regagent_backend.usage import UsageCallback


def _model(handle: Any, operation: str = "answer") -> tuple[ChatOpenAI, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return (
        ChatOpenAI(
            model="qwen-test",
            base_url="http://llm/v1",
            api_key=SecretStr("fake"),
            http_async_client=http,
            max_retries=0,
            disable_streaming=True,
            callbacks=[UsageCallback(operation)],
        ),
        http,
    )


def _completion(usage: dict[str, int] | None) -> httpx.Response:
    body: dict[str, Any] = {
        "id": "c",
        "created": 1,
        "object": "chat.completion",
        "model": "qwen-test",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _usage(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return [line for line in lines if line["event"] == "llm.usage"]


async def test_usage_from_provider_with_operation_and_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model, http = _model(
        lambda request: _completion(
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
        ),
        operation="guard",
    )
    async with http:
        await model.ainvoke(
            [HumanMessage("вопрос сотрудника")],
            config={"metadata": {"llm_operation": "comparison_check", "run_id": "run-1"}},
        )
    [event] = _usage(capsys)
    assert (event["status"], event["operation"], event["model"]) == (
        "ok",
        "comparison_check",
        "qwen-test",
    )
    assert (event["input_tokens"], event["output_tokens"], event["total_tokens"]) == (120, 30, 150)
    assert (event["usage_source"], event["run_id"]) == ("provider", "run-1")
    assert isinstance(event["duration_ms"], int)
    assert "вопрос" not in json.dumps(event, ensure_ascii=False)


async def test_default_operation_and_missing_usage_is_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model, http = _model(lambda request: _completion(None), operation="answer")
    async with http:
        await model.ainvoke([HumanMessage("x")])
    [event] = _usage(capsys)
    assert (event["operation"], event["usage_source"]) == ("answer", "unknown")
    assert "input_tokens" not in event


async def test_each_physical_call_is_one_event(capsys: pytest.CaptureFixture[str]) -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    model, http = _model(lambda request: _completion(usage))
    async with http:
        await model.ainvoke([HumanMessage("1")])
        await model.ainvoke([HumanMessage("2")])
    assert [event["total_tokens"] for event in _usage(capsys)] == [12, 12]


async def test_timeout_is_recorded_without_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    model, http = _model(slow)
    async with http:
        with pytest.raises(Exception):  # noqa: B017 — тип зависит от версии openai
            await model.ainvoke([HumanMessage("x")])
    [event] = _usage(capsys)
    assert (event["status"], event["usage_source"]) == ("timeout", "unknown")
    assert "Timeout" in event["error_code"]  # langchain-openai: OpenAITimeoutError
