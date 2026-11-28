from __future__ import annotations

import asyncio
import json
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
from regagent_knowledge.api import AdminDeps, ApiDeps, create_app
from regagent_knowledge.normalize import Clause
from regagent_knowledge.okf import ClauseView, RegulationView
from regagent_knowledge.registry import DocumentRecord, JobRecord, Registry

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


# --- служебный API (D-51) ----------------------------------------------------------


class JobLogFixture:
    def __init__(self) -> None:
        now = datetime(2026, 11, 27, 10, tzinfo=UTC)
        self.records = {
            "j-failed": JobRecord(
                "j-failed", "upsert", "finance", 2, "failed", 4, "сбой", None, {}, now, now
            ),
            "j-ok": JobRecord(
                "j-ok", "upsert", "public-doc", 2, "succeeded", 1, None, None, {}, now, now
            ),
        }
        self.retried: list[str] = []

    async def list_jobs(self, *, status: str | None, limit: int) -> list[JobRecord]:
        found = [job for job in self.records.values() if status is None or job.status == status]
        return found[:limit]

    async def get_job(self, job_id: str) -> JobRecord | None:
        return self.records.get(job_id)


@pytest.fixture
def admin_deps(fixture_deps: ApiDeps) -> tuple[ApiDeps, JobLogFixture]:
    log = JobLogFixture()

    async def retry(doc_id: str) -> int:
        log.retried.append(doc_id)
        return 1

    return replace(fixture_deps, admin=AdminDeps(log, retry)), log


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/documents"),
        ("GET", "/admin/jobs"),
        ("POST", "/admin/jobs/j-failed/retry"),
        ("GET", "/admin/review"),
    ],
)
def test_admin_requires_service_token(
    admin_deps: tuple[ApiDeps, JobLogFixture], method: str, path: str
) -> None:
    with TestClient(create_app(admin_deps[0])) as client:
        assert client.request(method, path).status_code == 401


def test_admin_lists_documents_of_every_level(admin_deps: tuple[ApiDeps, JobLogFixture]) -> None:
    with TestClient(create_app(admin_deps[0])) as client:
        body = client.get("/admin/documents", headers=HEADERS).json()
    assert {doc["doc_id"]: doc["level"] for doc in body["documents"]} == {
        "finance": "closed_fin",
        "public-doc": "public",
    }


def test_admin_lists_failed_jobs(admin_deps: tuple[ApiDeps, JobLogFixture]) -> None:
    with TestClient(create_app(admin_deps[0])) as client:
        body = client.get("/admin/jobs", params={"status": "failed"}, headers=HEADERS).json()
        too_many = client.get("/admin/jobs", params={"limit": 10_000}, headers=HEADERS)
    assert [job["job_id"] for job in body["jobs"]] == ["j-failed"]
    assert too_many.status_code == 422


def test_admin_retry_queues_failed_job_document_only(
    admin_deps: tuple[ApiDeps, JobLogFixture],
) -> None:
    deps, log = admin_deps
    with TestClient(create_app(deps)) as client:
        # Документ уже переиндексирован другой задачей: повторять нечего
        assert client.post("/admin/jobs/j-failed/retry", headers=HEADERS).status_code == 409

    class FailedRegistry(RegistryFixture):
        async def get(self, doc_id: str) -> DocumentRecord | None:
            record = await super().get(doc_id)
            return replace(record, status="failed") if record else None

    deps = replace(deps, registry=FailedRegistry())
    with TestClient(create_app(deps)) as client:
        queued = client.post("/admin/jobs/j-failed/retry", headers=HEADERS)
        done = client.post("/admin/jobs/j-ok/retry", headers=HEADERS)
        missing = client.post("/admin/jobs/nope/retry", headers=HEADERS)
    assert (queued.status_code, queued.json()) == (202, {"queued": 1})
    assert done.status_code == 409
    assert missing.status_code == 404
    assert log.retried == ["finance"]


def test_admin_review_lists_ambiguous_and_missing_links(fixture_deps: ApiDeps) -> None:
    class ReviewOkf(OkfFixture):
        def active_metadata(self) -> list[dict[str, Any]]:
            [first, second] = super().active_metadata()
            first["links_needing_review"] = [
                {"clause": "2.5", "via": "регламентом", "candidates": ["finance", "public-doc"]}
            ]
            second["missing_links"] = [{"clause": "4.4", "via": "Инструкцией по системе"}]
            return [first, second]

    deps = replace(
        fixture_deps,
        okf=ReviewOkf(historical_access="print_only"),
        admin=AdminDeps(JobLogFixture(), lambda doc_id: asyncio.sleep(0, 0)),
    )
    with TestClient(create_app(deps)) as client:
        items = client.get("/admin/review", headers=HEADERS).json()["items"]
    assert [(item["doc_id"], item["kind"], item["clause"]) for item in items] == [
        ("public-doc", "ambiguous", "2.5"),
        ("finance", "missing", "4.4"),
    ]
    assert items[0]["candidates"] == ["finance", "public-doc"]


def test_admin_without_admin_deps_is_unavailable(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        assert client.get("/admin/jobs", headers=HEADERS).status_code == 503


def test_admin_retry_reports_broken_manifest_as_conflict(fixture_deps: ApiDeps) -> None:
    class FailedRegistry(RegistryFixture):
        async def get(self, doc_id: str) -> DocumentRecord | None:
            record = await super().get(doc_id)
            return replace(record, status="failed") if record else None

    async def broken(doc_id: str) -> int:
        raise ValueError("переиндексация требует валидного манифеста")

    deps = replace(
        fixture_deps, registry=FailedRegistry(), admin=AdminDeps(JobLogFixture(), broken)
    )
    with TestClient(create_app(deps)) as client:
        response = client.post("/admin/jobs/j-failed/retry", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "переиндексация требует валидного манифеста"


# --- события (C3, D-52) ------------------------------------------------------------


def _events(capsys: pytest.CaptureFixture[str], name: str) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [line for line in lines if line["event"] == name]


def test_search_event_records_outcome_without_query(
    fixture_deps: ApiDeps, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        client.post(
            "/search",
            json={"query": "секретная формулировка", "levels": ["public"], "top_k": None},
            headers=HEADERS,
        )
    [event] = _events(capsys, "search.request")
    assert (event["service"], event["status"], event["fragments"]) == ("knowledge", "empty", 0)
    assert event["levels"] == 1
    assert "секретная" not in json.dumps(event, ensure_ascii=False)


def test_restricted_document_is_marked_only_in_service_logs(
    fixture_deps: ApiDeps, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        results = [
            client.post("/search", json={"query": query, "levels": levels}, headers=HEADERS)
            for query, levels in [
                ("Where is the Finance document?", ["public"]),
                ("Where is the finance-policy document?", ["public"]),
                ("Where is the Finance document?", ["public", "closed_fin"]),
            ]
        ]

    assert all(response.status_code == 200 for response in results)
    assert all(response.json() == results[0].json() for response in results)
    events = _events(capsys, "search.request")
    assert [event["status"] for event in events] == ["access_limited", "empty", "empty"]
    assert "Finance" not in repr(events) and "finance" not in repr(events)
    assert isinstance(fixture_deps.rag, RagFixture)
    assert [level for level, _, _ in fixture_deps.rag.calls] == [
        "public",
        "public",
        "public",
        "closed_fin",
    ]


def test_search_expands_project_lead_only_when_needed(fixture_deps: ApiDeps) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        for query in ("Кто назначает РП?", "Кто назначает руководителя проекта (РП)?"):
            response = client.post(
                "/search", json={"query": query, "levels": ["public"]}, headers=HEADERS
            )
            assert response.status_code == 200
    assert isinstance(fixture_deps.rag, RagFixture)
    assert fixture_deps.rag.calls == [
        ("public", "Кто назначает РП? (РП — руководитель проекта)", 3),
        ("public", "Кто назначает руководителя проекта (РП)?", 3),
    ]


def test_clause_and_refs_events_record_not_found(
    fixture_deps: ApiDeps, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(fixture_deps)) as client:
        client.post("/clause", json=CLAUSE, headers=HEADERS)
        client.post("/refs", json={"doc_id": "absent", "levels": ["public"]}, headers=HEADERS)
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    by_event = {line["event"]: line for line in lines}
    assert by_event["clause.request"]["status"] == "not_found"
    assert by_event["refs.request"]["status"] == "not_found"
    assert by_event["refs.request"]["doc_id"] == "absent"
