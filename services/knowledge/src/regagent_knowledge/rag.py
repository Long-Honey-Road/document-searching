"""Реестр объектов LightRAG: по объекту и workspace на уровень доступа (D-02, D-12).

Первым инициализируется пустой служебный workspace ``lvl__sentinel``: если какой-то путь
LightRAG упадёт на workspace по умолчанию процесса, он попадёт в пустоту, а не в чужой
уровень. Решения по работе с LightRAG — D-28…D-30 в docs/LLD.md.
"""

from __future__ import annotations

import configparser
import logging
import os
from asyncio import CancelledError, timeout
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from lightrag import QueryParam
from lightrag.base import DeletionResult, DocStatus

log = logging.getLogger(__name__)

SENTINEL_WORKSPACE = "lvl__sentinel"
# Разделитель фрагментов внутри документа, который отдаётся LightRAG (D-28). LightRAG режет
# ровно по нему; во фрагментах встречаться не может — проверяется при вставке.
FRAGMENT_SEPARATOR = "\n\n␞␞FRAGMENT␞␞\n\n"
_PROCESSED = "processed"
_UNFINISHED = [
    DocStatus.PENDING,
    DocStatus.PARSING,
    DocStatus.ANALYZING,
    DocStatus.PROCESSING,
    DocStatus.PREPROCESSED,
    DocStatus.FAILED,
]
_CANCEL_CLEANUP_TIMEOUT_S = 5.0


class RagError(RuntimeError):
    """Ошибка LightRAG: вставка не дошла до processed, запрос вернул failure и т.п."""


class UnknownLevelError(KeyError):
    pass


class WorkspaceOverrideError(RuntimeError):
    """Задано переопределение workspace, которое свело бы все уровни в один (D-30)."""


class DocStatusLike(Protocol):
    async def get_by_id(self, id: str) -> dict[str, Any] | None: ...


class LightRAGLike(Protocol):
    doc_status: Any

    async def initialize_storages(self) -> None: ...

    async def finalize_storages(self) -> None: ...

    async def ainsert(self, input: str, **kwargs: Any) -> str: ...

    async def adelete_by_doc_id(self, doc_id: str) -> DeletionResult: ...

    async def aquery_data(self, query: str, param: QueryParam) -> dict[str, Any]: ...


def assert_no_workspace_override(cwd: Path | None = None) -> None:
    """LightRAG даёт ``POSTGRES_WORKSPACE`` (или ``[postgres] workspace`` в ``config.ini``
    рабочего каталога) приоритет над workspace каждого экземпляра. Спайк P0-5: с ним все
    уровни пишут в один workspace. Любое такое переопределение — отказ стартовать."""
    if "POSTGRES_WORKSPACE" in os.environ:
        raise WorkspaceOverrideError(
            "задана переменная POSTGRES_WORKSPACE: она сводит все уровни доступа в один "
            "workspace LightRAG; уберите её из окружения knowledge"
        )
    ini = (cwd or Path.cwd()) / "config.ini"
    if ini.exists():
        parser = configparser.ConfigParser()
        parser.read(ini, encoding="utf-8")
        if parser.has_option("postgres", "workspace"):
            raise WorkspaceOverrideError(
                f"{ini}: [postgres] workspace сводит все уровни доступа в один workspace"
            )


class RagRegistry:
    def __init__(
        self,
        levels: Iterable[str],
        factory: Callable[[str], LightRAGLike],
        *,
        mode: str = "mix",
    ) -> None:
        self._levels = list(levels)
        self._factory = factory
        self._mode = mode
        self._rags: dict[str, LightRAGLike] = {}
        self._sentinel: LightRAGLike | None = None

    def workspace(self, level: str) -> str:
        if level not in self._levels:
            raise UnknownLevelError(level)
        return f"lvl_{level}"

    @property
    def levels(self) -> list[str]:
        return list(self._levels)

    async def start(self) -> None:
        assert_no_workspace_override()
        sentinel = self._factory(SENTINEL_WORKSPACE)
        await sentinel.initialize_storages()
        self._sentinel = sentinel
        for level in self._levels:
            rag = self._factory(self.workspace(level))
            await rag.initialize_storages()
            self._rags[level] = rag

    async def discard_unfinished(self) -> dict[str, list[str]]:
        """Удаляет из LightRAG документы, вставка которых не дошла до processed (D-50).

        После падения процесса посреди ``ainsert`` документ остаётся в pending/processing/
        failed, и LightRAG подхватил бы его при вставке любого другого документа. Реестр
        knowledge — источник истины: такой документ переиндексирует повторная задача."""
        discarded: dict[str, list[str]] = {}
        for level in self._levels:
            rag = self._rag(level)
            unfinished = await rag.doc_status.get_docs_by_statuses(_UNFINISHED)
            for doc_key in sorted(unfinished):
                await self.delete(level, doc_key)
            if unfinished:
                discarded[level] = sorted(unfinished)
        return discarded

    async def stop(self) -> None:
        for rag in [*self._rags.values(), *([self._sentinel] if self._sentinel else [])]:
            await rag.finalize_storages()
        self._rags.clear()
        self._sentinel = None

    def _rag(self, level: str) -> LightRAGLike:
        ws = self.workspace(level)
        rag = self._rags.get(level)
        if rag is None:
            raise RagError(f"реестр LightRAG не запущен (workspace {ws})")
        return rag

    async def insert(self, level: str, doc_key: str, chunks: Sequence[str]) -> None:
        """Заменяет документ ``doc_key`` в workspace уровня нашими фрагментами.

        LightRAG молча пропускает вставку документа с уже существующим id, поэтому сначала
        удаляем; ошибки обработки LightRAG не бросает, а пишет в doc_status — проверяем его.
        """
        if not chunks:
            raise ValueError("нет фрагментов для вставки")
        for chunk in chunks:
            if not chunk.strip():
                raise ValueError("пустой фрагмент")
            if FRAGMENT_SEPARATOR in chunk:
                raise ValueError("фрагмент содержит служебный разделитель")
        rag = self._rag(level)
        await self.delete(level, doc_key)
        try:
            await rag.ainsert(
                FRAGMENT_SEPARATOR.join(chunks),
                split_by_character=FRAGMENT_SEPARATOR,
                split_by_character_only=True,
                ids=doc_key,
                file_paths=doc_key,
            )
        except CancelledError:
            # LightRAG освобождает workspace, но следующий ainsert подхватит
            # незавершённый документ. Повторами управляет очередь knowledge.
            try:
                async with timeout(_CANCEL_CLEANUP_TIMEOUT_S):
                    await self.delete(level, doc_key)
            except Exception:
                log.exception("не удалось очистить отменённую вставку %s в %s", doc_key, level)
            raise
        status = await rag.doc_status.get_by_id(doc_key)
        state = (status or {}).get("status", "missing")
        if state != _PROCESSED:
            error = (status or {}).get("error_msg") or (status or {}).get("error") or ""
            raise RagError(
                f"LightRAG не проиндексировал {doc_key} в {self.workspace(level)}: {state} {error}"
            )

    async def delete(self, level: str, doc_key: str) -> None:
        result = await self._rag(level).adelete_by_doc_id(doc_key)
        if result.status not in {"success", "not_found"}:
            raise RagError(
                f"удаление {doc_key} из {self.workspace(level)}: {result.status} {result.message}"
            )

    async def query(
        self,
        level: str,
        query: str,
        top_k: int,
        *,
        keywords: Sequence[str] | None = None,
    ) -> list[str]:
        """Тексты чанков из workspace уровня. Сущности и связи LightRAG заголовка не несут
        и в выдачу не идут (слой 4 проверки прав их всё равно выбросил бы)."""
        kw = list(keywords) if keywords else [query]
        param = QueryParam(
            mode=self._mode,
            top_k=top_k,
            chunk_top_k=top_k,
            hl_keywords=kw,
            ll_keywords=kw,
            enable_rerank=False,
        )
        result = await self._rag(level).aquery_data(query, param=param)
        status = result.get("status")
        if status == "failure":
            # aquery_data возвращает failure только когда ничего не нашлось; настоящие
            # ошибки он бросает исключениями.
            log.debug("%s: %s", self.workspace(level), result.get("message"))
            return []
        if status != "success":
            raise RagError(f"запрос к {self.workspace(level)}: {status} {result.get('message')}")
        chunks = (result.get("data") or {}).get("chunks") or []
        return [c["content"] for c in chunks if isinstance(c.get("content"), str)]
