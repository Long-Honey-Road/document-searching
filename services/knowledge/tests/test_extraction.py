import asyncio
from pathlib import Path
from typing import Any

import pytest
from lightrag import LightRAG
from lightrag.operate import _process_extraction_result
from lightrag.utils import EmbeddingFunc, Tokenizer
from regagent_knowledge.lightrag_factory import LlmFunc, build_lightrag
from regagent_knowledge.rag import RagRegistry
from regagent_knowledge.settings import KnowledgeSettings

from .conftest import fake_llm


def test_factory_passes_configured_language_and_extraction_format(
    knowledge_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_func: LlmFunc,
    embedding_func: EmbeddingFunc,
    tokenizer: Tokenizer,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "regagent_knowledge.lightrag_factory.LightRAG", lambda **kwargs: calls.append(kwargs)
    )
    settings = KnowledgeSettings(lightrag_working_dir=tmp_path, lightrag_extraction_json=True)
    build_lightrag(
        "sample", settings, llm_func=llm_func, embedding_func=embedding_func, tokenizer=tokenizer
    )
    [call] = calls
    assert call["addon_params"] == {"language": "Russian"}
    assert call["entity_extraction_use_json"] is True


async def test_missing_completion_marker_retains_full_records_and_loses_a_broken_row() -> None:
    complete = (
        "entity<|#|>РУКОВОДИТЕЛЬ<|#|>роль<|#|>Назначает исполнителя\n"
        "entity<|#|>ИСПОЛНИТЕЛЬ<|#|>роль<|#|>Выполняет работы\n"
        "relation<|#|>РУКОВОДИТЕЛЬ<|#|>ИСПОЛНИТЕЛЬ<|#|>назначение<|#|>"
        "Руководитель назначает исполнителя"
    )
    expected = await _process_extraction_result(complete + "\n<|COMPLETE|>", "chunk", 1)
    intact = await _process_extraction_result(complete, "chunk", 1)
    truncated = await _process_extraction_result(complete.rsplit("<|#|>", 1)[0], "chunk", 1)
    assert intact == expected
    assert len(expected[0]) == 2 and len(expected[1]) == 1
    assert truncated[0] == expected[0] and not truncated[1]


async def test_lightrag_accepts_next_insert_after_cancellation(
    tmp_path: Path, embedding_func: EmbeddingFunc, tokenizer: Tokenizer
) -> None:
    enter_extraction = asyncio.Event()

    async def llm(prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
        if "MARK_STUCK" in prompt:
            enter_extraction.set()
            await asyncio.Event().wait()
        return await fake_llm(prompt, system_prompt, **kwargs)

    def factory(workspace: str) -> LightRAG:
        return LightRAG(
            workspace=workspace,
            working_dir=str(tmp_path),
            llm_model_func=llm,
            embedding_func=embedding_func,
            tokenizer=tokenizer,
            entity_extract_max_gleaning=0,
            llm_model_max_async=1,
        )

    registry = RagRegistry(["public"], factory)
    await registry.start()
    try:
        task = asyncio.create_task(registry.insert("public", "stuck@v1", ["MARK_STUCK"]))
        try:
            await asyncio.wait_for(enter_extraction.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(registry.insert("public", "next@v1", ["MARK_NEXT"]), 5)
    finally:
        await registry.stop()
