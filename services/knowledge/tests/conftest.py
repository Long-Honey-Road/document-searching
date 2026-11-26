"""Общие подделки для тестов knowledge: модель, эмбеддинги, токенизатор, Postgres."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

import numpy as np
import pytest
from lightrag.utils import EmbeddingFunc, Tokenizer

FAKE_DIM = 256

REQUIRED_ENV = {
    "LLM_BASE_URL": "http://llm:8000/v1",
    "LLM_MODEL": "m",
    "EMBEDDINGS_BASE_URL": "http://emb:80/v1",
    "EMBEDDINGS_MODEL": "bge-m3",
    "EMBEDDINGS_DIM": "1024",
    "RERANK_BASE_URL": "http://rerank:80",
    "RERANK_MODEL": "bge-reranker-v2-m3",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "KNOWLEDGE_DB": "knowledge",
    "KNOWLEDGE_DB_USER": "knowledge",
    "KNOWLEDGE_DB_PASSWORD": "secret",
}


@pytest.fixture
def knowledge_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # setenv+delenv: monkeypatch запомнит исходное состояние и вернёт его после теста, даже
    # если код под тестом выставит эти переменные сам (configure_postgres_env).
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE", "POSTGRES_WORKSPACE"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return monkeypatch


class CharTokenizer:
    """1 символ = 1 токен; не скачивает словарь tiktoken."""

    def encode(self, content: str) -> list[int]:
        return [ord(c) for c in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(map(chr, tokens))


def char_tokenizer() -> Tokenizer:
    return Tokenizer(model_name="chars", tokenizer=CharTokenizer())


def fake_embed_texts(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), FAKE_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in re.findall(r"\w+", t.lower()):
            out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % FAKE_DIM] += 1.0  # noqa: S324
        n = np.linalg.norm(out[i])
        if n:
            out[i] /= n
    return out


async def _fake_embed(texts: list[str], **_: Any) -> np.ndarray:
    return fake_embed_texts(texts)


def fake_embedding_func() -> EmbeddingFunc:
    return EmbeddingFunc(
        embedding_dim=FAKE_DIM, func=_fake_embed, max_token_size=8192, model_name="fake-embed"
    )


async def fake_llm(prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
    """Извлечение сущностей: по одной сущности на маркер MARK_* в тексте фрагмента."""
    text = (system_prompt or "") + "\n" + prompt
    if kwargs.get("keyword_extraction") or "high_level_keywords" in text:
        return json.dumps({"high_level_keywords": [], "low_level_keywords": []})
    if "<|COMPLETE|>" in text or "entity_description" in text:
        marks = sorted(set(re.findall(r"MARK_[A-Z0-9_]+", prompt)))
        rows = [f"entity<|#|>{m}<|#|>маркер<|#|>Маркер {m}" for m in marks]
        return "\n".join(rows) + "\n<|COMPLETE|>"
    return "сводка"


@pytest.fixture
def tokenizer() -> Tokenizer:
    return char_tokenizer()


@pytest.fixture
def embedding_func() -> EmbeddingFunc:
    return fake_embedding_func()


@pytest.fixture
def llm_func() -> Any:
    return fake_llm


def pg_test_env() -> dict[str, str]:
    """Postgres для integration-тестов: TEST_POSTGRES_HOST/PORT/USER/PASSWORD/DB.

    В CI их выставляет шаг, который поднимает Postgres из deploy/postgres. Без них
    integration-тесты падают, а не пропускаются: тихий пропуск тестов на утечки хуже.
    """
    keys = ("HOST", "PORT", "USER", "PASSWORD", "DB")
    missing = [f"TEST_POSTGRES_{k}" for k in keys if not os.environ.get(f"TEST_POSTGRES_{k}")]
    if missing:
        pytest.fail(f"для integration-тестов нужны переменные: {', '.join(missing)}")
    return {k: os.environ[f"TEST_POSTGRES_{k}"] for k in keys}


@pytest.fixture
def pg_env() -> dict[str, str]:
    return pg_test_env()
