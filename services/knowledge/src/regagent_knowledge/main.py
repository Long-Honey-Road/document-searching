"""Точка входа FastAPI. Поиск открывается после проверки изоляции и сверки корпуса."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import asyncpg
import httpx
from fastapi import FastAPI
from lightrag.utils import TiktokenTokenizer
from regagent_common.access import AccessLevels

from .api import AdminDeps, ApiDeps, create_app
from .canary import check_isolation
from .consumer import IndexQueue, run_consumer
from .lightrag_factory import build_lightrag, make_embedding_func, make_llm_func
from .okf import OkfStore
from .parsing import DoclingParser
from .pipeline import PipelineDeps, recover_after_restart
from .rag import RagRegistry
from .registry import Registry
from .reindex import queue_reindex
from .rerank import EmbeddingReranker, Reranker, TeiReranker
from .settings import RuntimeSettings
from .watcher import FolderSource, run_watcher


async def _retry_document(
    source: FolderSource, registry: Registry, queue: IndexQueue, doc_id: str
) -> int:
    return await queue_reindex(source, registry, queue, only_failed=True, doc_ids={doc_id})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = RuntimeSettings()
    logging.basicConfig(level=settings.log_level)
    levels = AccessLevels.from_yaml(settings.access_levels_file)
    tokenizer = TiktokenTokenizer()
    embedding_func = make_embedding_func(settings)
    rag = RagRegistry(
        levels.all(),
        partial(
            build_lightrag,
            settings=settings,
            llm_func=make_llm_func(settings),
            embedding_func=embedding_func,
            tokenizer=tokenizer,
        ),
        mode=settings.search_mode,
    )
    okf = OkfStore(settings.okf_dir)
    await asyncio.to_thread(okf.init)

    async with (
        asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.knowledge_db_user,
            password=settings.knowledge_db_password.get_secret_value(),
            database=settings.knowledge_db,
        ) as pool,
        httpx.AsyncClient(timeout=settings.llm_timeout_s) as http,
    ):
        reranker: Reranker = (
            EmbeddingReranker(embedding_func)
            if settings.rerank_mode == "embeddings"
            else TeiReranker(settings.rerank_base_url, http)
        )
        registry = Registry(pool)
        await registry.migrate()
        try:
            await rag.start()
            public_levels = levels.levels_for(())
            await check_isolation(
                rag,
                levels.all(),
                [level for level in levels.all() if level not in public_levels],
                top_k=settings.search_top_k,
            )
            await recover_after_restart(rag, registry)
            indexing = PipelineDeps(
                DoclingParser(), okf, rag, registry, settings, levels=levels, tokenizer=tokenizer
            )
            async with IndexQueue(settings.rabbitmq_url.get_secret_value()) as queue:
                ready = asyncio.Event()
                async with asyncio.TaskGroup() as workers:
                    consumer = workers.create_task(run_consumer(queue, indexing))
                    watcher = workers.create_task(
                        run_watcher(
                            settings.docs_dir,
                            levels,
                            registry,
                            queue,
                            debounce_s=settings.watch_debounce_s,
                            ready=ready,
                        )
                    )
                    await ready.wait()
                    app.state.deps = ApiDeps(
                        service_token=settings.knowledge_internal_token,
                        rag=rag,
                        registry=registry,
                        reranker=reranker,
                        okf=okf,
                        min_score=settings.rerank_min_score,
                        search_top_k=settings.search_top_k,
                        rerank_candidate_limit=settings.rerank_candidate_limit,
                        rerank_text_max_chars=settings.rerank_text_max_chars,
                        admin=AdminDeps(
                            registry,
                            partial(
                                _retry_document,
                                FolderSource(settings.docs_dir, levels),
                                registry,
                                queue,
                            ),
                        ),
                    )
                    try:
                        yield
                    finally:
                        app.state.deps = None
                        watcher.cancel()
                        consumer.cancel()
        finally:
            await rag.stop()


app = create_app(lifespan=lifespan)
