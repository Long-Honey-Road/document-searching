"""Учёт токенов (C4): событие ``llm.usage`` на каждый физический вызов модели.

Колбэк вешается на модель при создании, поэтому срабатывает и внутри Deep Agent. Операция
берётся из ``metadata.llm_operation`` вызова, иначе — значение по умолчанию модели. Ретраев
клиента нет (``max_retries=0``), поэтому вызов LangChain — один HTTP-запрос к провайдеру.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from regagent_common.events import timeout_status, usage_fields

from regagent_backend.events import events

_RUN_KEYS = ("run_id", "thread_id")


class UsageCallback(AsyncCallbackHandler):
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self._calls: dict[UUID, tuple[float, dict[str, str]]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        meta = metadata or {}
        fields = {
            "operation": str(meta.get("llm_operation") or self.operation),
            "model": str(meta.get("ls_model_name") or ""),
        }
        fields.update({key: str(meta[key]) for key in _RUN_KEYS if meta.get(key)})
        self._calls[run_id] = (time.perf_counter(), fields)

    def _pop(self, run_id: UUID) -> tuple[float, dict[str, str]]:
        started, fields = self._calls.pop(run_id, (time.perf_counter(), {}))
        return (time.perf_counter() - started) * 1000, fields

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        duration, fields = self._pop(run_id)
        counts: dict[str, object] | None = None
        generation = response.generations[0][0] if response.generations else None
        if isinstance(generation, ChatGeneration):
            usage = getattr(generation.message, "usage_metadata", None)
            if usage:
                counts = dict(usage)
                details = usage.get("input_token_details") or {}
                if isinstance(details, dict) and "cache_read" in details:
                    counts["cache_read"] = details["cache_read"]
        if counts is None and response.llm_output:
            raw = response.llm_output.get("token_usage")
            counts = dict(raw) if isinstance(raw, dict) else None
        events.emit_fields(
            "llm.usage", {**fields, **usage_fields(counts)}, status="ok", duration_ms=duration
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        duration, fields = self._pop(run_id)
        events.emit_fields(
            "llm.usage",
            {**fields, "usage_source": "unknown"},
            status=timeout_status(error),
            duration_ms=duration,
            error_code=type(error).__name__,
        )
