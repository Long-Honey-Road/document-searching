"""LightRAG на настоящем Postgres: изоляция workspace, вставка/замена/удаление.

Postgres — из TEST_POSTGRES_* (см. conftest.pg_test_env). Графовое хранилище — из
TEST_LIGHTRAG_GRAPH_STORAGE (по умолчанию PGGraphStorage, то есть AGE).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from regagent_knowledge.canary import check_isolation
from regagent_knowledge.lightrag_factory import build_lightrag
from regagent_knowledge.rag import RagError, RagRegistry
from regagent_knowledge.settings import KnowledgeSettings

pytestmark = pytest.mark.integration


@pytest.fixture
async def registry(
    knowledge_env: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_func: Any,
    embedding_func: Any,
    tokenizer: Any,
    pg_env: dict[str, str],
) -> AsyncIterator[RagRegistry]:
    knowledge_env.setenv("POSTGRES_HOST", pg_env["HOST"])
    knowledge_env.setenv("POSTGRES_PORT", pg_env["PORT"])
    knowledge_env.setenv("KNOWLEDGE_DB", pg_env["DB"])
    knowledge_env.setenv("KNOWLEDGE_DB_USER", pg_env["USER"])
    knowledge_env.setenv("KNOWLEDGE_DB_PASSWORD", pg_env["PASSWORD"])
    knowledge_env.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path))
    knowledge_env.setenv("LIGHTRAG_EXTRACT_GLEANING", "0")
    knowledge_env.setenv("CHUNK_MAX_TOKENS", "400")
    settings = KnowledgeSettings(_env_file=None)
    graph = os.environ.get("TEST_LIGHTRAG_GRAPH_STORAGE", "PGGraphStorage")
    run = uuid.uuid4().hex[:8]

    def factory(ws: str) -> Any:
        return build_lightrag(
            ws,
            settings,
            llm_func=llm_func,
            embedding_func=embedding_func,
            tokenizer=tokenizer,
            graph_storage=graph,
        )

    reg = RagRegistry([f"pub{run}", f"fin{run}"], factory, mode="mix")
    await reg.start()
    yield reg
    await reg.stop()


def _levels(reg: RagRegistry) -> tuple[str, str]:
    pub, fin = reg.levels
    return pub, fin


def _chunk(doc: str, clause: str, level: str, body: str) -> str:
    return f"⟦doc={doc} v=1 clause={clause} area=общий level={level}⟧ {doc} — {clause}\n{body}"


async def _seed(reg: RagRegistry) -> None:
    pub, fin = _levels(reg)
    await reg.insert(
        pub,
        "pubdoc@v1",
        [_chunk("pubdoc", "1", pub, "Маркер MARK_PUB, сроки согласования.")],
    )
    await reg.insert(
        fin,
        "findoc@v1",
        [_chunk("findoc", "1", fin, "Маркер MARK_FIN, лимиты бюджета.")],
    )


@pytest.mark.leak
async def test_two_workspaces_do_not_see_each_other(registry: RagRegistry) -> None:
    await _seed(registry)
    pub, fin = _levels(registry)
    for level, own, foreign in ((pub, "MARK_PUB", "MARK_FIN"), (fin, "MARK_FIN", "MARK_PUB")):
        own_hits = await registry.query(level, f"маркер {own}", 10)
        foreign_hits = await registry.query(level, f"маркер {foreign}", 10)
        assert any(own in t for t in own_hits)
        assert not any(foreign in t for t in own_hits + foreign_hits)


async def test_chunks_come_back_with_our_header(registry: RagRegistry) -> None:
    await _seed(registry)
    pub, _ = _levels(registry)
    hits = await registry.query(pub, "маркер MARK_PUB", 10)
    assert hits and all(t.startswith("⟦doc=pubdoc v=1 clause=1 ") for t in hits)


async def test_reinsert_same_key_replaces_content(registry: RagRegistry) -> None:
    await _seed(registry)
    pub, _ = _levels(registry)
    await registry.insert(pub, "pubdoc@v1", [_chunk("pubdoc", "1", pub, "Маркер MARK_NEW.")])
    hits = await registry.query(pub, "маркер MARK_NEW MARK_PUB", 10)
    assert any("MARK_NEW" in t for t in hits)
    assert not any("MARK_PUB" in t for t in hits)


async def test_delete_removes_only_in_own_workspace(registry: RagRegistry) -> None:
    await _seed(registry)
    pub, fin = _levels(registry)
    await registry.delete(fin, "pubdoc@v1")  # чужой id в чужом уровне — ничего не трогает
    assert any("MARK_PUB" in t for t in await registry.query(pub, "маркер MARK_PUB", 10))
    await registry.delete(pub, "pubdoc@v1")
    assert not any("MARK_PUB" in t for t in await registry.query(pub, "маркер MARK_PUB", 10))
    assert any("MARK_FIN" in t for t in await registry.query(fin, "маркер MARK_FIN", 10))


async def test_too_long_fragment_fails_instead_of_being_cut(registry: RagRegistry) -> None:
    pub, _ = _levels(registry)
    huge = _chunk("big", "1", pub, "слово " * 400)  # > 2 * CHUNK_MAX_TOKENS символов
    with pytest.raises(RagError):
        await registry.insert(pub, "big@v1", [huge])


@pytest.mark.leak
async def test_no_canary_leaks_between_levels(registry: RagRegistry) -> None:
    public, closed = registry.levels
    await check_isolation(registry, [public, closed], [closed])


async def test_discard_unfinished_removes_half_indexed_documents(registry: RagRegistry) -> None:
    """После падения knowledge посреди вставки LightRAG держит документ не в processed.
    Следующая вставка любого документа подхватила бы его молча (D-50)."""
    pub, _ = _levels(registry)
    await _seed(registry)
    half: Any = registry._rag(pub)  # настоящий LightRAG: нужна вставка без обработки
    await half.apipeline_enqueue_documents(
        _chunk("half", "1", pub, "Недоиндексированный пункт"), ids="half@v1", file_paths="half@v1"
    )
    assert (await half.doc_status.get_by_id("half@v1"))["status"] == "pending"

    assert await registry.discard_unfinished() == {pub: ["half@v1"]}
    assert await half.doc_status.get_by_id("half@v1") is None
    assert (await half.doc_status.get_by_id("pubdoc@v1"))["status"] == "processed"
    assert await registry.discard_unfinished() == {}
