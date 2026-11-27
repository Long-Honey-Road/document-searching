"""Реестр документов на настоящем Postgres (TEST_POSTGRES_*); каждый тест — в своей схеме."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from regagent_knowledge.registry import Registry

pytestmark = pytest.mark.integration


@pytest.fixture
async def registry(pg_env: dict[str, str]) -> AsyncIterator[Registry]:
    schema = f"test_{uuid.uuid4().hex[:10]}"
    conn_args = {
        "host": pg_env["HOST"],
        "port": int(pg_env["PORT"]),
        "user": pg_env["USER"],
        "password": pg_env["PASSWORD"],
        "database": pg_env["DB"],
    }
    admin = await asyncpg.connect(**conn_args)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(**conn_args, server_settings={"search_path": schema})
    reg = Registry(pool)
    await reg.migrate()
    yield reg
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


async def test_level_of_returns_none_for_unknown(registry: Registry) -> None:
    assert await registry.level_of("nope") is None


async def test_set_status_roundtrip(registry: Registry) -> None:
    await registry.set_status(
        "a", 2, "s1", "closed_fin", "indexed", areas=["ДИБ"], title="Документ"
    )
    rec = await registry.get("a")
    assert rec is not None
    assert (rec.version, rec.sha256, rec.level, rec.areas, rec.title, rec.status) == (
        2,
        "s1",
        "closed_fin",
        ["ДИБ"],
        "Документ",
        "indexed",
    )
    assert await registry.level_of("a") == "closed_fin"
    assert [r.doc_id for r in await registry.all()] == ["a"]


async def test_failed_status_keeps_error_and_previous_metadata(registry: Registry) -> None:
    await registry.set_status("a", 1, "s1", "public", "indexed", areas=["ДИБ"], title="Д")
    await registry.set_status("a", 1, "s1", "public", "failed", error="парсер упал")
    rec = await registry.get("a")
    assert rec is not None
    assert (rec.status, rec.error, rec.areas, rec.title) == ("failed", "парсер упал", ["ДИБ"], "Д")


async def test_deleted_document_has_no_level(registry: Registry) -> None:
    await registry.set_status("a", 1, "s1", "public", "indexed", areas=["ДИБ"], title="Д")
    await registry.set_status("a", 1, "s1", "public", "deleted")
    assert await registry.level_of("a") is None


async def test_stale_document_status_update_preserves_new_acl(registry: Registry) -> None:
    await registry.set_status("a", 1, "s1", "public", "indexing")
    snapshot = await registry.get("a")
    assert snapshot is not None
    assert await registry.level_of("a") is None

    await registry.set_status("a", 2, "s2", "closed_fin", "indexed")

    assert not await registry.transition_status(snapshot, "indexed")
    assert await registry.level_of("a") == "closed_fin"


async def test_unknown_status_rejected(registry: Registry) -> None:
    with pytest.raises(ValueError):
        await registry.set_status("a", 1, "s1", "public", "done")


async def test_migrate_is_idempotent(registry: Registry) -> None:
    await registry.migrate()
    await registry.migrate()
