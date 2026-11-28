import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import aio_pika
import pytest
from regagent_common import broker
from regagent_common.contracts import (
    PracticeBinding,
    PracticePublicationResult,
    PracticePublish,
    SearchRequest,
)
from regagent_common.fragments import parse_header
from regagent_knowledge.consumer import IndexQueue, run_practice_consumer
from regagent_knowledge.okf import OkfStore
from regagent_knowledge.practice import PracticeService
from regagent_knowledge.registry import DocumentRecord
from regagent_knowledge.search import search


@pytest.fixture
def publication() -> PracticePublish:
    return PracticePublish(
        ticket_id="ticket-1",
        practice_id="practice-1",
        revision=1,
        text="Добавьте сроки.",
        mode="clarifies",
        binds=[PracticeBinding(doc_id="example", version=1, clause="1.1")],
        approved_by="admin",
        approved_levels=["public"],
    )


@pytest.fixture
def service(tmp_path: Path) -> tuple[PracticeService, AsyncMock]:
    okf = OkfStore(tmp_path / "okf")
    okf.init()
    path = okf.root / "docs/example/v1.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ndoc_id: example\nversion: 1\nstatus: active\n---\n\n1.1. Правило.\n",
        encoding="utf-8",
    )
    registry, rag = AsyncMock(), AsyncMock()
    registry.get.return_value = DocumentRecord(
        "example", 1, "hash", "public", ["ДИБ"], "Пример", "indexed", None
    )
    return PracticeService(okf, registry, rag), rag


async def test_publication_is_searchable_and_redelivery_does_not_reindex(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    result = await practices.publish(publication)
    assert result.status == "published"
    level, key, chunks = rag.insert.await_args.args
    assert (level, key) == ("public", "practice:practice-1")
    header = parse_header(chunks[0])
    assert header is not None and header.mode == "clarifies"
    assert await practices.visible(header, chunks[0])
    assert (await practices.publish(publication)).status == "published"
    rag.insert.assert_awaited_once()


async def test_publication_without_document_access_is_rejected(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    denied = publication.model_copy(update={"approved_levels": ["closed_fin"]})
    result = await practices.publish(denied)
    assert (result.status, result.error_code) == ("failed", "binding_unavailable")
    rag.insert.assert_not_awaited()


@pytest.mark.leak
async def test_financial_practice_can_only_be_published_and_found_at_its_level(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    cast(AsyncMock, practices.registry.get).return_value = DocumentRecord(
        "example", 1, "hash", "closed_fin", ["ДИБ"], "Пример", "indexed", None
    )
    assert (await practices.publish(publication)).error_code == "binding_unavailable"
    financial = publication.model_copy(update={"approved_levels": ["public", "closed_fin"]})
    assert (await practices.publish(financial)).status == "published"
    level, _, chunks = rag.insert.await_args.args
    assert level == "closed_fin"
    rag.levels = ["public", "closed_fin"]
    rag.query.side_effect = lambda level, *_: chunks if level == "closed_fin" else []
    reranker = AsyncMock()
    reranker.rerank.return_value = [0.9]

    for levels in (["public"], ["public", "closed_fin"]):
        result = await search(
            SearchRequest(query="Добавьте сведения о сроках.", levels=levels),
            rag,
            practices.registry.get,
            reranker,
            min_score=0.2,
            practice_lookup=practices.visible,
        )
        if "closed_fin" not in levels:
            assert result.fragments == []
        else:
            [citation] = result.fragments
            assert citation.fragment_id == "practice:practice-1"
            assert citation.text == financial.text


async def test_review_is_required_when_source_revision_changes(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    assert (await practices.publish(publication)).status == "published"
    chunk = rag.insert.await_args.args[2][0]
    cast(AsyncMock, practices.registry.get).return_value = DocumentRecord(
        "example", 2, "new-hash", "public", ["ДИБ"], "Пример", "indexed", None
    )
    header = parse_header(chunk)
    assert header is not None
    assert not await practices.visible(header, chunk)


async def test_recheck_is_persisted_and_replayed_after_broker_error(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    assert (await practices.publish(publication)).status == "published"
    cast(AsyncMock, practices.registry.get).return_value = DocumentRecord(
        "example", 1, "hash", "closed_fin", ["ДИБ"], "Пример", "indexed", None
    )
    emit = AsyncMock(side_effect=[RuntimeError(), None])
    with pytest.raises(RuntimeError):
        await practices.review_changes(emit)
    saved = practices.okf.read_practice(publication.practice_id)
    assert saved is not None and saved.status == "review"
    assert saved.recheck is not None and saved.recheck.reason == "access_changed"
    await practices.review_changes(emit)
    await practices.review_changes(emit)
    assert emit.await_count == 2
    assert emit.await_args_list[0] == emit.await_args_list[1]
    rag.delete.assert_awaited()


@pytest.mark.integration
async def test_broker_delivers_publication_and_persistent_result(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.fail("для integration-теста нужен TEST_RABBITMQ_URL отдельного брокера")
    practices, rag = service
    async with IndexQueue(url) as queue, await aio_pika.connect(url) as connection:
        channel = await connection.channel()
        exchange = await channel.get_exchange("regagent")
        # Собственная подписка получает только результаты, отправленные после её создания.
        # Общая очередь backend может содержать результаты предыдущих тестов.
        results = await channel.declare_queue(exclusive=True, auto_delete=True)
        await results.bind(exchange, routing_key=broker.PRACTICE_PUBLISHED)
        worker = asyncio.create_task(
            run_practice_consumer(queue, practices, retries=0, timeout_s=5)
        )
        try:
            async with results.iterator() as messages:
                await exchange.publish(
                    aio_pika.Message(publication.model_dump_json().encode()),
                    routing_key="practice.publish",
                )
                delivered = await asyncio.wait_for(anext(messages), 10)
                async with delivered.process():
                    result = PracticePublicationResult.model_validate_json(delivered.body)
                    assert result.status == "published"
                    assert result.ticket_id == publication.ticket_id
                    assert delivered.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
                rag.insert.assert_awaited_once()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.leak
async def test_all_bindings_checked_at_publication_and_search(
    service: tuple[PracticeService, AsyncMock], publication: PracticePublish
) -> None:
    practices, rag = service
    other = practices.okf.root / "docs/other/v1.md"
    other.parent.mkdir()
    other.write_text(
        "---\ndoc_id: other\nversion: 1\nstatus: active\n---\n\n1.1. Другое правило.\n",
        encoding="utf-8",
    )
    example = DocumentRecord("example", 1, "hash", "public", ["ДИБ"], "Пример", "indexed", None)
    closed = DocumentRecord("other", 1, "hash", "closed_fin", ["общий"], "Правила", "indexed", None)
    documents = {"example": example, "other": closed}
    cast(AsyncMock, practices.registry.get).side_effect = documents.get
    command = publication.model_copy(
        update={
            "binds": [*publication.binds, PracticeBinding(doc_id="other", version=1, clause="1.1")],
            "approved_levels": ["public", "closed_fin"],
        }
    )
    assert (await practices.publish(command)).status == "failed"
    rag.insert.assert_not_awaited()

    documents["other"] = replace(closed, level="public")
    assert (await practices.publish(command)).status == "published"
    chunks = rag.insert.await_args.args[2]
    common = next(chunk for chunk in chunks if "area=общий " in chunk)
    header = parse_header(common)
    assert header is not None and header.doc_id == "other"
    assert await practices.visible(header, common)
    dib = next(chunk for chunk in chunks if "area=ДИБ " in chunk)
    dib_header = parse_header(dib)
    assert dib_header is not None
    documents["other"] = closed
    assert not await practices.visible(dib_header, dib)
