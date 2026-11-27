"""Продолжение уточняющих диалогов и текст подсказок по ОКК."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import ValidationError
from regagent_common.contracts import AnswerMeta

CLARIFY_PREFIX = "УТОЧНЕНИЕ:"
NO_ANSWER = "НЕТ_ОТВЕТА"
WAITING = frozenset({"ask_area", "ask_clarification"})
MAX_DETAIL_QUESTIONS = 2
QC_GUIDE = (
    "Нажмите «Передать в ОКК» рядом с исходным вопросом, "
    "при необходимости добавьте комментарий и подтвердите отправку."
)
NOT_FOUND = (
    "В доступных материалах не нашёл сведений, которые однозначно отвечают на этот вопрос.\n\n"
    f"{QC_GUIDE} По вашей просьбе могу составить черновик заявки."
)
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>()#!])")


def parse_clarification(text: str) -> str | None:
    prefix, _, question = text.partition(":")
    if prefix + ":" != CLARIFY_PREFIX:
        return None
    return valid_clarification(question)


def valid_clarification(question: str) -> str | None:
    question = question.strip()
    if (
        4 <= len(question) <= 280
        and question.endswith("?")
        and question.count("?") == 1
        and not any(char in question for char in "\n\r<>[]!")
        and not re.search(r"\.\s", question)
    ):
        return question
    return None


def stored_meta(message: BaseMessage) -> AnswerMeta | None:
    if not isinstance(message, AIMessage):
        return None
    try:
        return AnswerMeta.model_validate(message.additional_kwargs.get("answer_meta"))
    except ValidationError:
        return None


@dataclass(frozen=True)
class Topic:
    questions: list[str]
    clarification_count: int


def current_topic(messages: Sequence[BaseMessage], limit: int) -> Topic:
    questions = []
    detail_count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            questions.append(message.text.strip()[:limit])
            if len(questions) == 6:
                break
        elif isinstance(message, AIMessage):
            meta = stored_meta(message)
            if meta is None or meta.outcome not in WAITING:
                break
            detail_count += meta.outcome == "ask_clarification"
    return Topic(list(reversed(questions)), detail_count)


def qc_draft(messages: Sequence[BaseMessage], limit: int) -> str | None:
    for index in range(len(messages) - 2, -1, -1):
        message = messages[index]
        if not isinstance(message, AIMessage):
            continue
        meta = stored_meta(message)
        if meta and meta.outcome == "qc_help":
            continue
        if meta is None or meta.outcome not in WAITING | {"not_found"}:
            return None
        questions = current_topic(messages[:index], limit).questions
        if questions:
            original, *details = (_MARKDOWN.sub(r"\\\1", line) for line in questions)
            text = f"**Черновик заявки в ОКК**\n\nПрошу уточнить порядок по вопросу: {original}"
            if details:
                text += "\n\nЧто уже уточнено: " + " ".join(details)
            return f"{text}\n\n{QC_GUIDE}"
        return None
    return None
