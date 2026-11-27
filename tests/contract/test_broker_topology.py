"""Knowledge и backend объявляют общие очереди одинаково (иначе PRECONDITION_FAILED)."""

from __future__ import annotations

from typing import Any

from regagent_backend import qc_worker
from regagent_common import broker
from regagent_knowledge import consumer


class FakeQueue:
    def __init__(self, name: str, log: set[tuple[str, str]]) -> None:
        self.name = name
        self._log = log

    async def bind(self, exchange: Any, routing_key: str) -> None:
        self._log.add((self.name, routing_key))


class FakeChannel:
    def __init__(self) -> None:
        self.declared: dict[str, dict[str, Any]] = {}
        self.bindings: set[tuple[str, str]] = set()

    async def declare_queue(
        self, name: str, *, durable: bool, arguments: dict[str, Any] | None = None
    ) -> FakeQueue:
        previous = self.declared.setdefault(name, dict(arguments or {}))
        assert previous == dict(arguments or {}), f"{name} объявлена дважды по-разному"
        return FakeQueue(name, self.bindings)


async def test_both_services_declare_shared_queues_identically() -> None:
    knowledge, backend = FakeChannel(), FakeChannel()
    await consumer.declare_topology(knowledge, exchange=object())
    await qc_worker.declare_topology(backend, exchange=object())
    shared = {spec.name for spec in broker.SHARED_QUEUES}
    assert shared <= set(knowledge.declared) and shared <= set(backend.declared)
    for name in set(knowledge.declared) & set(backend.declared):
        assert knowledge.declared[name] == backend.declared[name], name
    # Объявления обоих сервисов в одном брокере не конфликтуют
    both = FakeChannel()
    await consumer.declare_topology(both, exchange=object())
    await qc_worker.declare_topology(both, exchange=object())
    assert (broker.QC_EVENTS, broker.PRACTICE_PUBLISHED) in both.bindings
