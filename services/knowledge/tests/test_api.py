from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from regagent_knowledge.api import ApiDeps, create_app
from regagent_knowledge.normalize import Clause
from regagent_knowledge.okf import ClauseView, RegulationView
from regagent_knowledge.registry import DocumentRecord, Registry

HEADERS = {"X-Service-Token": "test-token"}
CLAUSE = {"doc_id": "finance", "clause": "1", "levels": ["public"]}


class RegistryFixture:
    async def get(self, doc_id: str) -> DocumentRecord | None:
        if doc_id not in {"finance", "public-doc"}:
            return None
        level = "closed_fin" if doc_id == "finance" else "public"
        return DocumentRecord(doc_id, 2, "sha", level, ["ДИБ"], f"Title {doc_id}", "indexed", None)

    async def all(self) -> list[DocumentRecord]:
        records = []
        for doc_id in ("finance", "public-doc"):
            if (record := await self.get(doc_id)) is not None:
                records.append(record)
        return records


class RagFixture:
    def __init__(self) -> None:
        self.levels = ["public", "closed_fin"]
        self.calls: list[tuple[str, str, int]] = []

    async def query(self, level: str, query: str, top_k: int) -> list[str]:
        self.calls.append((level, query, top_k))
        return []


class RerankerFixture:
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [0.9] * len(texts)


class OkfFixture:
    def __init__(self, historical_access: str) -> None:
        self.historical_access = historical_access
        self.reads: list[tuple[str, str, int]] = []

    def read_regulation(self, doc_id: str, version: int | None = None) -> RegulationView | None:
        return None

    def read_clause(self, doc_id: str, clause: str, version: int | None = None) -> ClauseView:
        assert version is not None
        self.reads.append((doc_id, clause, version))
        access = self.historical_access if version == 1 else "closed_fin"
        return ClauseView(
            entry_meta={
                "doc_id": doc_id,
                "version": version,
                "title": "Financial regulations",
                "status": "deprecated" if version == 1 else "active",
                "access": access,
                "areas": ["ДИБ"],
            },
            clause=Clause("1", "First section", "Rule text", 1),
            neighbors=[Clause("2", "Second section", "Neighbor rule", 1)],
        )

    def active_metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "doc_id": "public-doc",
                "version": 2,
                "title": "Public regulations",
                "status": "active",
                "access": "public",
                "links": [{"doc_id": "finance", "clause": "4", "via": "financial regulations"}],
            },
            {
                "doc_id": "finance",
                "version": 2,
                "title": "Financial regulations",
                "status": "active",
                "access": "closed_fin",
                "links": [{"doc_id": "public-doc", "clause": "1", "via": "public regulations"}],
            },
        ]


@pytest.fixture
def fixture_deps() -> ApiDeps:
    return ApiDeps(
        service_token=SecretStr("test-token"),
        rag=RagFixture(),
        registry=RegistryFixture(),
        reranker=RerankerFixture(),
        okf=OkfFixture(historical_access="print_only"),
        min_score=0.2,
        search_top_k=3,
    )


@pytest.mark.parametrize("endpoint", ["/search", "/clause", "/refs"])
def test_missing_service_token_is_401(fixture_deps: ApiDeps, endpoint: str) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.post(
            endpoint, content=b"not JSON", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401


def test_invalid_service_token_is_401(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.post("/clause", json=CLAUSE, headers={"X-Service-Token": "wrong"})
    assert response.status_code == 401


def test_health_without_token_ok(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_search_uses_configured_top_k_when_null(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.post(
            "/search",
            json={"query": "approval", "levels": ["public"], "top_k": None},
            headers=HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["fragments"] == []
    assert isinstance(fixture_deps.rag, RagFixture)
    assert fixture_deps.rag.calls == [("public", "approval", 3)]


@pytest.mark.leak
def test_clause_without_access_is_indistinguishable_from_unknown(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        denied = client.post("/clause", json=CLAUSE, headers=HEADERS)
        unknown = client.post("/clause", json={**CLAUSE, "doc_id": "absent"}, headers=HEADERS)
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json()
    assert isinstance(fixture_deps.okf, OkfFixture)
    assert fixture_deps.okf.reads == []


def test_clause_returns_current_version_and_neighbors(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.post(
            "/clause",
            json={**CLAUSE, "levels": ["closed_fin"]},
            headers=HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["fragment"]["fragment_id"] == "finance@2#1"
    assert data["fragment"]["text"] == "Rule text"
    assert data["neighbors"][0]["fragment_id"] == "finance@2#2"


@pytest.mark.leak
def test_clause_checks_access_of_requested_historical_version(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        response = client.post(
            "/clause",
            json={**CLAUSE, "levels": ["closed_fin"], "version": 1},
            headers=HEADERS,
        )
    assert response.status_code == 404


@pytest.mark.leak
def test_refs_filters_incoming_and_outgoing_by_registry(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        query = {"doc_id": "public-doc"}
        public = client.post("/refs", json={**query, "levels": ["public"]}, headers=HEADERS)
        finance = client.post(
            "/refs", json={**query, "levels": ["public", "closed_fin"]}, headers=HEADERS
        )
    assert public.status_code == finance.status_code == 200
    assert public.json() == {"outgoing": [], "incoming": []}
    assert finance.json() == {
        "outgoing": [
            {
                "doc_id": "finance",
                "title": "Financial regulations",
                "clause": "4",
                "via": "financial regulations",
            }
        ],
        "incoming": [
            {
                "doc_id": "finance",
                "title": "Financial regulations",
                "clause": "1",
                "via": "public regulations",
            }
        ],
    }


@pytest.mark.leak
def test_service_does_not_start_when_startup_canary_leaks(
    knowledge_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from regagent_knowledge import main
    from regagent_knowledge.canary import CanaryError

    access_file = tmp_path / "access.yaml"
    access_file.write_text(
        'levels:\n  public: {groups: ["*"]}\n  closed_fin: {groups: ["/finance"]}\n',
        encoding="utf-8",
    )
    knowledge_env.setenv("ACCESS_LEVELS_FILE", str(access_file))
    knowledge_env.setenv("OKF_DIR", str(tmp_path / "okf"))
    knowledge_env.setenv("RABBITMQ_URL", "amqp://rabbitmq")
    knowledge_env.setenv("KNOWLEDGE_INTERNAL_TOKEN", "test-token")

    @asynccontextmanager
    async def pool_fixture(**kwargs: Any) -> AsyncIterator[object]:
        yield object()

    async def migrate_fixture(registry: object) -> None:
        pass

    class CollapsedRag:
        def __init__(self, levels: list[str], factory: object, mode: str) -> None:
            self.chunks: list[str] = []

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def insert(self, level: str, key: str, chunks: Sequence[str]) -> None:
            self.chunks.extend(chunks)

        async def query(self, level: str, query: str, top_k: int) -> list[str]:
            return self.chunks

    def verify_queue_stays_closed(url: str) -> None:
        pytest.fail("RabbitMQ opened before canary check passed")

    knowledge_env.setattr(main, "TiktokenTokenizer", object)
    knowledge_env.setattr(main, "RagRegistry", CollapsedRag)
    knowledge_env.setattr(asyncpg, "create_pool", pool_fixture)
    knowledge_env.setattr(Registry, "migrate", migrate_fixture)
    knowledge_env.setattr(main, "IndexQueue", verify_queue_stays_closed)

    app = create_app(lifespan=main.lifespan)
    with pytest.raises(CanaryError, match="public"), TestClient(app):
        pytest.fail("Startup completed with a leaking workspace")
    assert app.state.deps is None


@pytest.mark.leak
def test_clause_revoked_while_reading_is_not_returned(fixture_deps: ApiDeps) -> None:
    class RevokingRegistry(RegistryFixture):
        def __init__(self) -> None:
            self.checked = False

        async def get(self, doc_id: str) -> DocumentRecord | None:
            if self.checked:
                return None
            self.checked = True
            return await super().get(doc_id)

    deps = replace(fixture_deps, registry=RevokingRegistry())
    with TestClient(create_app(deps)) as client:
        response = client.post(
            "/clause", json={**CLAUSE, "levels": ["closed_fin"]}, headers=HEADERS
        )
    assert response.status_code == 404


@pytest.mark.leak
def test_search_drops_old_same_level_version_after_rollover(fixture_deps: ApiDeps) -> None:
    class StaleRag(RagFixture):
        async def query(self, level: str, query: str, top_k: int) -> list[str]:
            return [
                f"⟦doc=finance v={version} clause=1 area=ДИБ level=closed_fin⟧"
                f" Finance — 1 Revision {version}\nRule for edition {version}."
                for version in (1, 2)
            ]

    deps = replace(fixture_deps, rag=StaleRag())
    with TestClient(create_app(deps)) as client:
        response = client.post(
            "/search", json={"query": "approval", "levels": ["closed_fin"]}, headers=HEADERS
        )
    assert response.status_code == 200
    assert [row["version"] for row in response.json()["fragments"]] == [2]


@pytest.mark.leak
@pytest.mark.parametrize("sha_changes", [True, False], ids=["new-content", "same-hash-reindex"])
def test_search_drops_same_version_text_replaced_during_retrieval(
    fixture_deps: ApiDeps, sha_changes: bool
) -> None:
    class UpdatingRegistry(RegistryFixture):
        updated = False

        async def get(self, doc_id: str) -> DocumentRecord | None:
            record = await super().get(doc_id)
            if record is None:
                return None
            return replace(
                record,
                sha256="new" if self.updated and sha_changes else "old",
                updated_at=datetime(2026, 11, 27, 10, int(self.updated), tzinfo=UTC),
            )

    registry = UpdatingRegistry()

    class UpdatingRag(RagFixture):
        async def query(self, level: str, query: str, top_k: int) -> list[str]:
            registry.updated = True
            return ["⟦doc=finance v=2 clause=1 area=ДИБ level=closed_fin⟧ Obsolete rule."]

    class ObservingReranker(RerankerFixture):
        texts_seen: tuple[str, ...] = ()

        async def rerank(self, query: str, texts: list[str]) -> list[float]:
            self.texts_seen = tuple(texts)
            return await super().rerank(query, texts)

    reranker = ObservingReranker()
    deps = replace(fixture_deps, rag=UpdatingRag(), registry=registry, reranker=reranker)
    with TestClient(create_app(deps)) as client:
        response = client.post(
            "/search", json={"query": "approval", "levels": ["closed_fin"]}, headers=HEADERS
        )
    assert response.status_code == 200
    assert response.json()["fragments"] == []
    assert reranker.texts_seen == ()


@pytest.mark.leak
def test_search_ignores_okf_text_with_different_hash(fixture_deps: ApiDeps) -> None:
    class CurrentRag(RagFixture):
        async def query(self, level: str, query: str, top_k: int) -> list[str]:
            return ["⟦doc=public-doc v=2 clause=6 area=ДИБ level=public⟧ Section"]

    class StaleOkf(OkfFixture):
        def read_regulation(self, doc_id: str, version: int | None = None) -> RegulationView:
            return RegulationView(
                {"status": "active", "access": "public", "sources": [{"sha256": "old"}]},
                [Clause("2.4", "Archive", "Archive: stale text", 2)],
            )

    deps = replace(fixture_deps, rag=CurrentRag(), okf=StaleOkf(historical_access="public"))
    with TestClient(create_app(deps)) as client:
        response = client.post(
            "/search", json={"query": "Archive", "levels": ["public"]}, headers=HEADERS
        )
    assert response.status_code == 200
    assert [fragment["clause"] for fragment in response.json()["fragments"]] == ["6"]
    assert "stale text" not in response.text
