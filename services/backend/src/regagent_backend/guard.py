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


class GuardResult(BaseModel):
    on_topic: bool
    injection: bool
    partial_topic: str | None
    explicit_areas: list[str]
    search_queries: list[str]
    memory_action: Literal["none", "remember", "forget"]


async def classify(
    model: BaseChatModel,
    questions: list[str],
    area_codes: list[str],
    awaiting_area: bool,
    mode: Literal["json_schema", "tool"],
) -> GuardResult:
    classifier = (
        model.with_structured_output(GuardResult, method="json_schema", strict=True)
        if mode == "json_schema"
        else model.with_structured_output(GuardResult, method="function_calling")
    )
    prompt = (PROMPTS / "guard.md").read_text(encoding="utf-8")
    task = json.dumps(
        {"области": area_codes, "ждём уточнения области": awaiting_area, "сообщения": questions},
        ensure_ascii=False,
    )

    async def invoke() -> object:
        # Изолируем события провайдера и результат классификации от стрима Aegra.
        return await create_task(
            classifier.ainvoke(
                [SystemMessage(prompt), HumanMessage(task)], config={"tags": [TAG_NOSTREAM]}
            ),
            context=Context(),
        )

    try:
        response = await invoke()
    except APITimeoutError:
        raise
    except APIConnectionError:
        response = await invoke()
    if not isinstance(response, GuardResult):
        raise ValueError("invalid guard response")
    return response
