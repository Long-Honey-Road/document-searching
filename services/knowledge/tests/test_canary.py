from collections.abc import Sequence

import pytest
from regagent_knowledge.canary import CanaryError, canary_token, check_isolation, find_leaks


class FakeRag:
    def __init__(self, *, visible_across_levels: bool = False, returns_hits: bool = True) -> None:
        self.visible_across_levels = visible_across_levels
        self.returns_hits = returns_hits
        self.chunks: dict[str, list[str]] = {}

    async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None:
        self.chunks[level] = list(chunks)

    async def query(self, level: str, query: str, top_k: int) -> list[str]:
        if not self.returns_hits:
            return []
        if self.visible_across_levels:
            return [text for chunks in self.chunks.values() for text in chunks]
        return self.chunks.get(level, [])


@pytest.mark.leak
async def test_find_leaks_detects_foreign_marker() -> None:
    rag = FakeRag(visible_across_levels=True)
    rag.chunks["closed_fin"] = [f"only in closed finance: {canary_token('closed_fin')}"]
    assert await find_leaks(rag, ["public", "closed_fin"]) == [("public", "closed_fin")]


@pytest.mark.leak
async def test_check_isolation_fails_startup_on_leak() -> None:
    rag = FakeRag(visible_across_levels=True)
    with pytest.raises(CanaryError, match="public"):
        await check_isolation(rag, ["public", "closed_fin"], ["closed_fin"])


@pytest.mark.leak
async def test_check_isolation_fails_when_seeded_canary_is_not_found() -> None:
    rag = FakeRag(returns_hits=False)
    with pytest.raises(CanaryError, match="closed_fin"):
        await check_isolation(rag, ["public", "closed_fin"], ["closed_fin"])


@pytest.mark.leak
async def test_check_isolation_seeds_closed_levels_and_allows_startup_when_isolated() -> None:
    rag = FakeRag()
    await check_isolation(rag, ["public", "closed_fin"], ["closed_fin"])
    assert rag.chunks.keys() == {"closed_fin"}
