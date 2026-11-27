"""Сборка объекта LightRAG на workspace и функций модели/эмбеддингов из настроек."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import numpy as np
from lightrag import LightRAG
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, Tokenizer

from .settings import KnowledgeSettings, configure_postgres_env

LlmFunc = Callable[..., Awaitable[str]]
# Ключ для локальных серверов без аутентификации: клиент OpenAI требует непустой ключ.
_NO_KEY = "not-needed"


def build_lightrag(
    workspace: str,
    settings: KnowledgeSettings,
    *,
    llm_func: LlmFunc,
    embedding_func: EmbeddingFunc,
    tokenizer: Tokenizer | None,
    graph_storage: str = "PGGraphStorage",
) -> LightRAG:
    """``tokenizer=None`` — tiktoken LightRAG; на стенде его словарь должен лежать в кэше
    образа (D-31). ``chunk_token_size`` берётся с запасом: LightRAG не должен дорезать наши
    фрагменты, а при превышении бросает ошибку (D-28)."""
    configure_postgres_env(settings)
    return LightRAG(
        working_dir=str(settings.lightrag_working_dir),
        workspace=workspace,
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        graph_storage=graph_storage,
        doc_status_storage="PGDocStatusStorage",
        llm_model_func=llm_func,
        llm_model_name=settings.model_for("extract"),
        llm_model_max_async=settings.llm_max_concurrency,
        embedding_func=embedding_func,
        tokenizer=tokenizer,
        chunk_token_size=settings.chunk_max_tokens * 2,
        chunk_overlap_token_size=0,
        entity_extract_max_gleaning=settings.lightrag_extract_gleaning,
        vector_db_storage_cls_kwargs={"cosine_better_than_threshold": 0.0},
    )


def make_llm_func(settings: KnowledgeSettings) -> LlmFunc:
    async def llm(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        extra = {**settings.llm_extra_body_json, **kwargs.pop("extra_body", {})}
        answer: str = await openai_complete_if_cache(
            settings.model_for("extract"),
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value() or _NO_KEY,
            timeout=int(settings.llm_timeout_s),
            extra_body=extra or None,
            **kwargs,
        )
        return answer

    return llm


def make_embedding_func(settings: KnowledgeSettings) -> EmbeddingFunc:
    raw: Callable[..., Awaitable[np.ndarray]] = openai_embed.func
    return EmbeddingFunc(
        embedding_dim=settings.embeddings_dim,
        func=partial(
            raw,
            model=settings.embeddings_model,
            base_url=settings.embeddings_base_url,
            api_key=_NO_KEY,
        ),
        max_token_size=8192,
        model_name=settings.embeddings_model,
    )
