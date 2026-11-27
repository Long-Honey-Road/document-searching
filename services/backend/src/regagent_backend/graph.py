"""Граф агента. Временная заглушка до P1-C4: отвечает одним проверенным сообщением.

Нужна, чтобы поднять backend в compose и проверить вход и чат до готовности агента.
Форма итогового сообщения — как у настоящего графа (D-19).
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from regagent_backend.identity import current_identity


class State(TypedDict):
    messages: Annotated[list[Any], add_messages]


async def finalize(state: State, config: RunnableConfig) -> dict[str, Any]:
    ident = current_identity(config)
    meta: dict[str, Any] = {
        "verified": True,
        "area_used": None,
        "area_source": "none",
        "sources": [],
        "outcome": "not_found",
    }
    text = f"Агент ещё не подключён. Вы вошли как {ident.username}."
    return {"messages": [AIMessage(content=text, additional_kwargs={"answer_meta": meta})]}


_builder = StateGraph(State)
_builder.add_node("finalize", finalize)
_builder.add_edge(START, "finalize")
_builder.add_edge("finalize", END)
graph = _builder.compile()
