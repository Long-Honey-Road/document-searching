"""Топология RabbitMQ для очередей, которые объявляют оба сервиса (D-61).

RabbitMQ отвечает ``PRECONDITION_FAILED``, если очередь объявлена повторно с другими
аргументами, поэтому knowledge и backend берут описание отсюда, а не пишут своё.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

EXCHANGE = "regagent"
DLQ = "regagent.dlq"
PRACTICE_PUBLISH = "practice.publish"
PRACTICE_PUBLISHED = "practice.published"
QC_EVENTS = "qc.events"
QC_EVENTS_DLQ = "qc.events.dlq"


@dataclass(frozen=True)
class QueueSpec:
    name: str
    routing_keys: tuple[str, ...]
    arguments: Mapping[str, Any] = field(default_factory=dict)


def _dead_letters_to(queue: str) -> dict[str, Any]:
    return {"x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": queue}


SHARED_QUEUES: tuple[QueueSpec, ...] = (
    QueueSpec(DLQ, (DLQ,)),
    QueueSpec(QC_EVENTS_DLQ, (QC_EVENTS_DLQ,)),
    # Команды ОКК → knowledge
    QueueSpec(PRACTICE_PUBLISH, (PRACTICE_PUBLISH,), _dead_letters_to(DLQ)),
    # Результаты публикации и перепроверки → backend, по порядку
    QueueSpec(
        QC_EVENTS,
        (QC_EVENTS, PRACTICE_PUBLISHED),
        {**_dead_letters_to(QC_EVENTS_DLQ), "x-single-active-consumer": True},
    ),
)


async def declare_shared(channel: Any, exchange: Any) -> dict[str, Any]:
    """Объявляет общие очереди на канале aio-pika; возвращает их по имени."""
    queues: dict[str, Any] = {}
    for spec in SHARED_QUEUES:
        queue = await channel.declare_queue(
            spec.name, durable=True, arguments=dict(spec.arguments) or None
        )
        for key in spec.routing_keys:
            await queue.bind(exchange, routing_key=key)
        queues[spec.name] = queue
    return queues
