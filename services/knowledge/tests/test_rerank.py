import json
from typing import Any

import httpx
import pytest
from regagent_knowledge.rerank import TeiReranker


async def test_tei_maps_ranked_results_to_input_order() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/rerank"
        payload = json.loads(request.content)
        assert payload["query"] == "who approves"
        assert payload["texts"] == ["first", "second"]
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.3}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        scores = await TeiReranker("http://rerank", http).rerank(
            "who approves", ["first", "second"]
        )
    assert scores == [0.3, 0.9]


@pytest.mark.parametrize(
    "ranks",
    [
        [{"index": 0, "score": 0.1}],
        [{"index": 1, "score": 0.9}, {"index": 1, "score": 0.8}],
        [{"index": -1, "score": 0.9}, {"index": 0, "score": 0.1}],
    ],
)
async def test_tei_rejects_partial_or_inconsistent_response(ranks: list[dict[str, Any]]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ranks)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ValueError):
            await TeiReranker("http://rerank", http).rerank("who approves", ["first", "second"])


async def test_tei_batches_large_candidate_sets_in_input_order() -> None:
    batches = []

    def handle(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        batches.append(len(texts))
        return httpx.Response(
            200,
            json=[
                {"index": i, "score": float(text)} for i, text in reversed(list(enumerate(texts)))
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        scores = await TeiReranker("http://rerank", http).rerank(
            "who approves", [str(i) for i in range(34)]
        )
    assert batches == [32, 2]
    assert scores == list(range(34))


async def test_embedding_reranker_reuses_one_batch_and_preserves_order() -> None:
    import numpy as np
    from lightrag.utils import EmbeddingFunc
    from regagent_knowledge.rerank import EmbeddingReranker

    async def embed(texts: list[str]) -> Any:
        assert texts == ["назначает РП", "Срок поставки", "Координатор назначает РП"]
        return np.array([[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]])

    ranker = EmbeddingReranker(EmbeddingFunc(embedding_dim=2, func=embed))
    scores = await ranker.rerank("назначает РП", ["Срок поставки", "Координатор назначает РП"])
    assert scores[0] == 0.0
    assert scores[1] > 0.9


async def test_general_phrase_does_not_promote_weak_embedding_match() -> None:
    import numpy as np
    from lightrag.utils import EmbeddingFunc
    from regagent_knowledge.rerank import EmbeddingReranker

    async def embed(texts: list[str]) -> Any:
        return np.array([[1.0, 0.0], [0.6, 0.8]])

    ranker = EmbeddingReranker(EmbeddingFunc(embedding_dim=2, func=embed))
    scores = await ranker.rerank(
        "Через какой срок заключение по расходам передаётся после подписания в закрытый архив?",
        ["После подписания начинается разработка проекта."],
    )
    assert scores == [0.0]
