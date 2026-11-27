"""Классификация вопроса и явно названных пользователем областей."""

from __future__ import annotations

import json
from asyncio import create_task
from contextvars import Context
from pathlib import Path
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.constants import TAG_NOSTREAM
from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel

PROMPTS = Path(__file__).with_name("prompts")
StructuredMode = Literal["json_schema", "tool"]


class GuardResult(BaseModel):
    on_topic: bool
    injection: bool
    partial_topic: str | None
    explicit_areas: list[str]
    search_queries: list[str]
    memory_action: Literal["none", "remember", "forget"]
    qc_action: Literal["none", "draft", "guide"]
    missing_detail: str | None
    area_only: bool


async def invoke_structured[T: BaseModel](
    model: BaseChatModel,
    schema: type[T],
    mode: StructuredMode,
    prompt: str,
    data: dict[str, object],
    metadata: dict[str, str] | None = None,
) -> T:
    classifier = (
        model.with_structured_output(schema, method="json_schema", strict=True)
        if mode == "json_schema"
        else model.with_structured_output(schema, method="function_calling")
    )
    task = json.dumps(data, ensure_ascii=False)

    async def invoke() -> object:
        # Изолируем события провайдера и результат классификации от стрима Aegra.
        return await create_task(
            classifier.ainvoke(
                [SystemMessage(prompt), HumanMessage(task)],
                config={"tags": [TAG_NOSTREAM], "metadata": dict(metadata or {})},
            ),
            context=Context(),
        )

    try:
        response = await invoke()
    except APITimeoutError:
        raise
    except APIConnectionError:
        response = await invoke()
    if not isinstance(response, schema):
        raise ValueError(f"invalid structured response for {schema.__name__}")
    return response


async def classify(
    model: BaseChatModel,
    questions: list[str],
    area_codes: list[str],
    awaiting_area: bool,
    mode: StructuredMode,
    run: dict[str, str] | None = None,
    *,
    awaiting_detail: bool = False,
) -> GuardResult:
    return await invoke_structured(
        model,
        GuardResult,
        mode,
        (PROMPTS / "guard.md").read_text(encoding="utf-8"),
        {
            "области": area_codes,
            "ждём уточнения области": awaiting_area,
            "ждём уточнения условия": awaiting_detail,
            "сообщения": questions,
        },
        {"llm_operation": "guard", **(run or {})},
    )
