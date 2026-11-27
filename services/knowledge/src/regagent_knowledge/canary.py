"""Стартовая проверка изоляции LightRAG по служебным маркерам (D-12)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from typing import Protocol

from .settings import DEFAULT_SEARCH_TOP_K


class CanaryError(RuntimeError):
    """Изоляция workspace не подтверждена."""


class CanarySearch(Protocol):
    async def query(self, level: str, query: str, top_k: int) -> list[str]: ...


class CanaryIndex(CanarySearch, Protocol):
    async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None: ...


def canary_token(level: str) -> str:
    suffix = hashlib.sha256(level.encode()).hexdigest()[:24].upper()
    return f"MARK_CANARY_{suffix}"


async def find_leaks(
    rag: CanarySearch,
    levels: list[str],
    *,
    top_k: int = DEFAULT_SEARCH_TOP_K,
) -> list[tuple[str, str]]:
    probes = [
        (level, owner, canary_token(owner))
        for level in levels
        for owner in levels
        if level != owner
    ]
    results = await asyncio.gather(
        *(rag.query(level, token, top_k) for level, _, token in probes),
    )
    return [
        (level, owner)
        for (level, owner, token), chunks in zip(probes, results, strict=True)
        if any(token in chunk for chunk in chunks)
    ]


async def check_isolation(
    rag: CanaryIndex,
    levels: list[str],
    closed_levels: list[str],
    *,
    top_k: int = DEFAULT_SEARCH_TOP_K,
) -> None:
    for level in closed_levels:
        token = canary_token(level)
        # Идентификаторы пользовательских документов не могут начинаться с подчёркивания.
        doc_key = f"_canary-{level}@v1"
        await rag.insert(level, doc_key, [f"Служебный маркер изоляции workspace: {token}"])

    for level in closed_levels:
        token = canary_token(level)
        if not any(token in chunk for chunk in await rag.query(level, token, top_k)):
            raise CanaryError(f"канарейка не найдена в своём workspace: {level}")

    leaks = await find_leaks(rag, levels, top_k=top_k)
    if leaks:
        raise CanaryError(f"чужие канарейки в результатах поиска: {leaks}")
