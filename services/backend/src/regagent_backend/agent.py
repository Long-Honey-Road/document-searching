"""Создание Deep Agent с инструментами, разрешёнными для конкретного запуска."""

from __future__ import annotations

from asyncio import create_task
from collections.abc import Awaitable, Callable
from contextvars import Context
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.constants import TAG_NOSTREAM

from regagent_backend.identity import UserIdentity, identity_to_user_dict


class AllowedTools(AgentMiddleware[Any, Any, Any]):
    def __init__(self, tools: list[BaseTool]) -> None:
        self.allowed_names = {tool.name for tool in tools}

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        tools: list[BaseTool | dict[str, Any]] = [
            tool
            for tool in request.tools
            if isinstance(tool, BaseTool) and tool.name in self.allowed_names
        ]
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        if request.tool_call["name"] not in self.allowed_names:
            return ToolMessage(
                content="Инструмент недоступен.",
                tool_call_id=request.tool_call["id"] or "",
                status="error",
            )
        return await handler(request)


async def generate_answer(
    model: BaseChatModel,
    prompt: str,
    messages: list[AnyMessage],
    tools: list[BaseTool],
    identity: UserIdentity,
    max_steps: int,
    run: dict[str, str] | None = None,
) -> list[AnyMessage]:
    agent = create_deep_agent(
        model,
        tools,
        system_prompt=prompt,
        middleware=[FilesystemMiddleware(tools=["read_file"]), AllowedTools(tools)],
        checkpointer=False,
    )
    # The inner graph has its own context so its states and tool outputs cannot
    # reach the public graph stream, even when the caller requests subgraphs.
    config: RunnableConfig = {
        "configurable": {"langgraph_auth_user": identity_to_user_dict(identity), **(run or {})},
        "recursion_limit": max_steps,
        "tags": [TAG_NOSTREAM],
        "metadata": {"llm_operation": "answer", **(run or {})},
    }
    inputs: InputAgentState = {"messages": list(messages)}
    result = await create_task(agent.ainvoke(inputs, config=config), context=Context())
    return list(result["messages"])


def last_answer(messages: list[AnyMessage]) -> AIMessage | None:
    last = messages[-1] if messages else None
    return last if isinstance(last, AIMessage) and not last.tool_calls else None
