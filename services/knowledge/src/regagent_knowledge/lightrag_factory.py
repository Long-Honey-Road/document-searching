"""Сборка объекта LightRAG на workspace и функций модели/эмбеддингов из настроек."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from lightrag import LightRAG
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, Tokenizer
from regagent_common.events import Value, timeout_status, usage_fields

from .events import CURRENT_JOB, events
from .settings import KnowledgeSettings, configure_postgres_env

LlmFunc = Callable[..., Awaitable[str]]
_raw_embed: Callable[..., Awaitable[np.ndarray]] = openai_embed.func


class _CallUsage:
    """``token_tracker`` LightRAG на один вызов: запоминает usage провайдера, если он есть.
    Ответ из кэша LightRAG не вызывает нашу функцию — токенов не добавляет."""

    def __init__(self, upstream: Any = None) -> None:
        self.counts: dict[str, object] | None = None
        self._upstream = upstream

    def add_usage(self, token_counts: dict[str, object]) -> None:
        self.counts = dict(token_counts)
        if self._upstream is not None:
            self._upstream.add_usage(token_counts)


def _emit_usage(
    operation: str,
    model: str,
    started: float,
    usage: _CallUsage,
    error: BaseException | None = None,
    **extra: int,
) -> None:
    fields: dict[str, Value] = {"operation": operation, "model": model, **extra}
    if (job := CURRENT_JOB.get()) is not None:
        fields["job_id"] = job
    fields.update(usage_fields(usage.counts if error is None else None))
    events.emit_fields(
        "llm.usage",
        fields,
        status="ok" if error is None else timeout_status(error),
        duration_ms=(time.perf_counter() - started) * 1000,
        error_code=type(error).__name__ if error is not None else None,
    )


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
        addon_params={"language": settings.lightrag_language},
        entity_extraction_use_json=settings.lightrag_extraction_json,
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
        model = settings.model_for("extract")
        operation = "keywords" if kwargs.get("keyword_extraction") else "index"
        usage = _CallUsage(kwargs.pop("token_tracker", None))
        started = time.perf_counter()
        try:
            answer: str = await openai_complete_if_cache(
                model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value() or _NO_KEY,
                timeout=int(settings.llm_timeout_s),
                extra_body=extra or None,
                token_tracker=usage,
                **kwargs,
            )
        except BaseException as exc:
            _emit_usage(operation, model, started, usage, exc)
            raise
        _emit_usage(operation, model, started, usage)
        return answer

    return llm


def make_embedding_func(settings: KnowledgeSettings) -> EmbeddingFunc:
    model = settings.embeddings_model

    async def embed(texts: list[str], **kwargs: Any) -> np.ndarray:
        usage = _CallUsage(kwargs.pop("token_tracker", None))
        started = time.perf_counter()
        try:
            vectors = await _raw_embed(
                texts,
                model=model,
                base_url=settings.embeddings_base_url,
                api_key=_NO_KEY,
                token_tracker=usage,
                **kwargs,
            )
        except BaseException as exc:
            _emit_usage("embedding", model, started, usage, exc, items=len(texts))
            raise
        _emit_usage("embedding", model, started, usage, items=len(texts))
        return vectors

    return EmbeddingFunc(
        embedding_dim=settings.embeddings_dim,
        func=embed,
        max_token_size=8192,
        model_name=settings.embeddings_model,
    )
