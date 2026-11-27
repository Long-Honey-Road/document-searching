"""Реранк фрагментов. Возвращаем оценки в порядке входа."""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Protocol

import httpx
import numpy as np
from lightrag.utils import EmbeddingFunc
from pydantic import BaseModel, Field, TypeAdapter


class Reranker(Protocol):
    async def rerank(self, query: str, texts: list[str]) -> list[float]: ...


_WORD = re.compile(r"[^\W\d_]{2,}")
_QUESTION_MARKERS = {"через", "какой", "какая", "какие", "каков", "после", "сколь"}


def search_tokens(text: str) -> list[str]:
    return [word.casefold()[:5] for word in _WORD.findall(text) if len(word) >= 4 or word.isupper()]


class EmbeddingReranker:
    def __init__(self, embed: EmbeddingFunc) -> None:
        self._embed = embed

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        vectors = np.asarray(await self._embed([query, *texts]), dtype=np.float64)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts) + 1:
            raise ValueError("некорректный пакет эмбеддингов реранка")
        query_vector = vectors[0]
        query_norm = float(np.linalg.norm(query_vector))
        query_words = search_tokens(query)
        query_terms = set(query_words) - _QUESTION_MARKERS
        query_pairs = set(pairwise(query_words))
        scores = []
        for text, vector in zip(texts, vectors[1:], strict=True):
            norm = query_norm * float(np.linalg.norm(vector))
            similarity = float(np.dot(query_vector, vector)) / norm if norm else 0.0
            # Сдвиг шкалы отсекает фоновые совпадения эмбеддингов.
            score = (similarity - 0.45) / 0.55
            words = search_tokens(text)
            if score < 0.35 and len(query_terms.intersection(words)) < 0.4 * len(query_terms):
                scores.append(0.0)
                continue
            matches = len(query_pairs.intersection(pairwise(words)))
            scores.append(max(0.0, min(1.0, score + 0.08 * min(2, matches))))
        return scores


class _Rank(BaseModel):
    index: int = Field(ge=0, strict=True)
    score: float = Field(allow_inf_nan=False, strict=True)


_RANKS = TypeAdapter(list[_Rank])
_TEI_BATCH_SIZE = 32


class TeiReranker:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._url = f"{base_url.rstrip('/')}/rerank"
        self._http = http

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        scores = []
        for start in range(0, len(texts), _TEI_BATCH_SIZE):
            scores.extend(await self._rank_batch(query, texts[start : start + _TEI_BATCH_SIZE]))
        return scores

    async def _rank_batch(self, query: str, texts: list[str]) -> list[float]:
        response = await self._http.post(
            self._url,
            json={"query": query, "texts": texts, "truncate": True},
        )
        response.raise_for_status()
        ranks = _RANKS.validate_json(response.content)
        by_index = {rank.index: rank.score for rank in ranks}
        if len(ranks) != len(texts) or by_index.keys() != set(range(len(texts))):
            raise ValueError("TEI вернул неполный набор оценок")
        return [by_index[i] for i in range(len(texts))]
