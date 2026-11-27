"""Общая топология RabbitMQ (D-61): очереди, которые объявляют и knowledge, и backend."""

from __future__ import annotations

from typing import Any

from regagent_common import broker


class FakeQueue:
    def __init__(self, name: str, log: list[tuple[str, str]]) -> None:
        self.name = name
        self._log = log

    async def bind(self, exchange: Any, routing_key: str) -> None:
        self._log.append((self.name, routing_key))


class FakeChannel:
    def __init__(self) -> None:
        self.declared: dict[str, dict[str, Any]] = {}
        self.bindings: list[tuple[str, str]] = []

    async def declare_queue(
        self, name: str, *, durable: bool, arguments: dict[str, Any] | None = None
    ) -> FakeQueue:
        assert durable
        self.declared[name] = dict(arguments or {})
        return FakeQueue(name, self.bindings)


async def declared() -> FakeChannel:
    channel = FakeChannel()
    queues = await broker.declare_shared(channel, exchange=object())
    assert set(queues) == set(channel.declared)
    return channel


async def test_every_dead_letter_route_reaches_a_declared_queue() -> None:
    channel = await declared()
    for arguments in channel.declared.values():
        if "x-dead-letter-routing-key" in arguments:
            assert arguments["x-dead-letter-exchange"] == broker.EXCHANGE
            target = arguments["x-dead-letter-routing-key"]
            assert (target, target) in channel.bindings


async def test_publication_results_and_rechecks_land_in_one_backend_queue() -> None:
    channel = await declared()
    assert (broker.QC_EVENTS, broker.PRACTICE_PUBLISHED) in channel.bindings
    assert (broker.QC_EVENTS, broker.QC_EVENTS) in channel.bindings
    assert channel.declared[broker.QC_EVENTS]["x-single-active-consumer"] is True
    # Отдельной очереди результатов нет: её никто не читает
    assert broker.PRACTICE_PUBLISHED not in channel.declared


async def test_publish_commands_dead_letter_to_common_dlq() -> None:
    channel = await declared()
    assert channel.declared[broker.PRACTICE_PUBLISH]["x-dead-letter-routing-key"] == broker.DLQ
    assert (broker.PRACTICE_PUBLISH, broker.PRACTICE_PUBLISH) in channel.bindings
