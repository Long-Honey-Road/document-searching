"""Внутренние HTTP-маршруты knowledge."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import SecretStr
from regagent_common.contracts import (
    AdminDocument,
    AdminDocuments,
    AdminJob,
    AdminJobs,
    AdminReview,
    ClauseRequest,
    ClauseResponse,
    DocumentRef,
    Fragment,
    RefsRequest,
    RefsResponse,
    RetryResponse,
    ReviewItem,
    SearchRequest,
    SearchResponse,
)
from regagent_common.fragments import FragmentHeader, fragment_id
from starlette.types import Lifespan

from .events import events
from .manifest import COMMON_AREA
from .registry import DocumentRecord, JobRecord
from .rerank import Reranker
from .search import ChunkSearch, search
from .settings import (
    DEFAULT_RERANK_CANDIDATE_LIMIT,
    DEFAULT_RERANK_TEXT_MAX_CHARS,
    DEFAULT_SEARCH_TOP_K,
)

if TYPE_CHECKING:
    from .normalize import Clause
from .okf import ClauseView, RegulationView


class DocumentAccess(Protocol):
    async def get(self, doc_id: str) -> DocumentRecord | None: ...

    async def all(self) -> list[DocumentRecord]: ...


class ClauseStore(Protocol):
    def read_regulation(self, doc_id: str, version: int | None = None) -> RegulationView | None: ...

    def read_clause(
        self, doc_id: str, clause: str, version: int | None = None
    ) -> ClauseView | None: ...

    def active_metadata(self) -> list[dict[str, Any]]: ...


class JobLog(Protocol):
    async def list_jobs(self, *, status: str | None, limit: int) -> list[JobRecord]: ...

    async def get_job(self, job_id: str) -> JobRecord | None: ...


@dataclass(frozen=True)
class AdminDeps:
    """Служебный API: журнал задач и повтор упавшего документа (``reindex --failed``
    для одного документа)."""

    jobs: JobLog
    retry: Callable[[str], Awaitable[int]]


@dataclass(frozen=True)
class ApiDeps:
    service_token: SecretStr
    rag: ChunkSearch
    registry: DocumentAccess
    reranker: Reranker
    okf: ClauseStore
    min_score: float
    search_top_k: int = DEFAULT_SEARCH_TOP_K
    rerank_candidate_limit: int = DEFAULT_RERANK_CANDIDATE_LIMIT
    rerank_text_max_chars: int = DEFAULT_RERANK_TEXT_MAX_CHARS
    admin: AdminDeps | None = None


def _deps(app: FastAPI) -> ApiDeps:
    deps: ApiDeps | None = app.state.deps
    if deps is None:
        raise HTTPException(status_code=503, detail="Knowledge not ready")
    return deps


def _active_record(record: DocumentRecord | None, allowed: set[str]) -> bool:
    return (
        record is not None
        and record.status == "indexed"
        and record.version is not None
        and record.level in allowed
    )


def _clause_fragment(
    row: DocumentRecord, meta: dict[str, Any], clause: Clause, version: int
) -> Fragment:
    areas = meta.get("areas") or [COMMON_AREA]
    header = FragmentHeader(row.doc_id, version, clause.number, areas[0], row.level or "")
    return Fragment(
        fragment_id=fragment_id(header),
        doc_id=row.doc_id,
        version=version,
        title=meta["title"],
        clause=clause.number,
        area=header.area,
        kind="regulation",
        text=clause.text,
        score=1,
    )


async def _read_clause(req: ClauseRequest, deps: ApiDeps) -> ClauseResponse:
    denied = HTTPException(status_code=404, detail="Clause not found")
    record = await deps.registry.get(req.doc_id)
    allowed = set(req.levels).intersection(deps.rag.levels)
    if record is None or not _active_record(record, allowed):
        raise denied
    version = record.version if req.version is None else req.version
    if version is None:
        raise denied

    view = await asyncio.to_thread(deps.okf.read_clause, req.doc_id, req.clause, version)
    if view is None:
        raise denied
    meta = view.entry_meta
    expected_status = "active" if version == record.version else "deprecated"
    if (
        meta.get("doc_id") != record.doc_id
        or meta.get("version") != version
        or meta.get("status") != expected_status
        or meta.get("access") != record.level
        or not isinstance(meta.get("title"), str)
    ):
        raise denied
    if await deps.registry.get(req.doc_id) != record:
        raise denied
    return ClauseResponse(
        fragment=_clause_fragment(record, meta, view.clause, version),
        neighbors=[_clause_fragment(record, meta, clause, version) for clause in view.neighbors],
    )


def _refs_to(source: dict[str, Any], targets: dict[str, dict[str, Any]]) -> list[DocumentRef]:
    refs = []
    for link in source.get("links") or []:
        doc_id = link["doc_id"]
        if doc_id in targets:
            refs.append(
                DocumentRef(
                    doc_id=doc_id,
                    title=targets[doc_id]["title"],
                    clause=link["clause"],
                    via=link["via"],
                )
            )
    return refs


async def _read_refs(req: RefsRequest, deps: ApiDeps) -> RefsResponse:
    denied = HTTPException(status_code=404, detail="Document not found")
    allowed = set(req.levels).intersection(deps.rag.levels)
    if not _active_record(await deps.registry.get(req.doc_id), allowed):
        raise denied
    metadata = await asyncio.to_thread(deps.okf.active_metadata)
    visible = {}
    for meta in metadata:
        if meta["access"] not in allowed:
            continue
        record = await deps.registry.get(meta["doc_id"])
        if (
            record is not None
            and _active_record(record, allowed)
            and record.level == meta["access"]
            and record.version == meta["version"]
        ):
            visible[record.doc_id] = meta
    origin = visible.get(req.doc_id)
    if origin is None:
        raise denied
    incoming = []
    for doc_id, meta in visible.items():
        for ref in _refs_to(meta, {req.doc_id: origin}):
            incoming.append(ref.model_copy(update={"doc_id": doc_id, "title": meta["title"]}))
    return RefsResponse(outgoing=_refs_to(origin, visible), incoming=incoming)


def create_app(
    deps: ApiDeps | None = None, *, lifespan: Lifespan[FastAPI] | None = None
) -> FastAPI:
    app = FastAPI(lifespan=lifespan, openapi_url=None)
    app.state.deps = deps

    @app.middleware("http")
    async def service_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path != "/health":
            current: ApiDeps | None = app.state.deps
            token = current.service_token.get_secret_value() if current is not None else ""
            headers = request.headers.getlist("x-service-token")
            if (
                not token
                or len(headers) != 1
                or not hmac.compare_digest(headers[0].encode(), token.encode())
            ):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        _deps(app)
        return {"status": "ok"}

    @app.post("/search")
    async def search_route(req: SearchRequest) -> SearchResponse:
        current = _deps(app)

        async def read_indexed_clauses(record: DocumentRecord) -> list[Clause]:
            view = await asyncio.to_thread(
                current.okf.read_regulation, record.doc_id, record.version
            )
            if view is None:
                return []
            metadata = view.entry_meta
            sources = metadata.get("sources")
            if (
                metadata.get("access") != record.level
                or metadata.get("status") != "active"
                or record.sha256 is None
                or not isinstance(sources, list)
                or not any(
                    isinstance(source, dict) and source.get("sha256") == record.sha256
                    for source in sources
                )
            ):
                return []
            return view.clauses

        with events.timed("search.request", levels=len(req.levels), areas=req.areas) as event:
            response = await search(
                req,
                current.rag,
                current.registry.get,
                current.reranker,
                current.min_score,
                default_top_k=current.search_top_k,
                registry_snapshot=current.registry.all,
                indexed_clauses=read_indexed_clauses,
                rerank_candidate_limit=current.rerank_candidate_limit,
                rerank_text_max_chars=current.rerank_text_max_chars,
            )
            event["status"] = "ok" if response.fragments else "empty"
            event["fragments"] = len(response.fragments)
            event["low_confidence"] = response.low_confidence
            event["areas_diverge"] = response.areas_diverge
        return response

    @app.post("/clause")
    async def clause_route(req: ClauseRequest) -> ClauseResponse:
        with events.timed("clause.request", doc_id=req.doc_id, levels=len(req.levels)) as event:
            try:
                return await _read_clause(req, _deps(app))
            except HTTPException as exc:
                # 404 — нет документа или нет прав, неразличимо (07, слой «утечка через ошибки»)
                event["status"] = "not_found" if exc.status_code == 404 else "rejected"
                event["http_status"] = exc.status_code
                raise

    @app.post("/refs")
    async def refs_route(req: RefsRequest) -> RefsResponse:
        with events.timed("refs.request", doc_id=req.doc_id, levels=len(req.levels)) as event:
            try:
                response = await _read_refs(req, _deps(app))
            except HTTPException as exc:
                event["status"] = "not_found" if exc.status_code == 404 else "rejected"
                event["http_status"] = exc.status_code
                raise
            event["outgoing"], event["incoming"] = len(response.outgoing), len(response.incoming)
            event["status"] = "ok" if response.outgoing or response.incoming else "empty"
            return response

    def _admin() -> tuple[ApiDeps, AdminDeps]:
        current = _deps(app)
        if current.admin is None:
            raise HTTPException(status_code=503, detail="Admin API not ready")
        return current, current.admin

    @app.get("/admin/documents")
    async def admin_documents() -> AdminDocuments:
        current, _ = _admin()
        records = sorted(await current.registry.all(), key=lambda record: record.doc_id)
        return AdminDocuments(
            documents=[
                AdminDocument(
                    doc_id=r.doc_id,
                    version=r.version,
                    level=r.level,
                    areas=r.areas,
                    title=r.title,
                    status=r.status,
                    error=r.error,
                    updated_at=r.updated_at,
                )
                for r in records
            ]
        )

    @app.get("/admin/jobs")
    async def admin_jobs(
        status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> AdminJobs:
        _, admin = _admin()
        found = await admin.jobs.list_jobs(status=status, limit=limit)
        return AdminJobs(jobs=[AdminJob.model_validate(asdict(job)) for job in found])

    @app.post("/admin/jobs/{job_id}/retry", status_code=202)
    async def admin_retry(job_id: str) -> RetryResponse:
        current, admin = _admin()
        job = await admin.jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        record = await current.registry.get(job.doc_id)
        if job.status != "failed" or record is None or record.status != "failed":
            raise HTTPException(status_code=409, detail="Document is not failed")
        try:
            queued = await admin.retry(job.doc_id)
        except ValueError as exc:  # манифест или папка документов не позволяют повтор
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RetryResponse(queued=queued)

    @app.get("/admin/review")
    async def admin_review() -> AdminReview:
        current, _ = _admin()
        metadata = await asyncio.to_thread(current.okf.active_metadata)
        items: list[ReviewItem] = []
        for meta in metadata:
            base = {"doc_id": meta["doc_id"], "version": meta["version"], "title": meta["title"]}
            for link in meta.get("links_needing_review") or []:
                items.append(
                    ReviewItem(
                        **base,
                        clause=link["clause"],
                        via=link["via"],
                        kind="ambiguous",
                        candidates=list(link.get("candidates") or []),
                    )
                )
            for link in meta.get("missing_links") or []:
                items.append(
                    ReviewItem(**base, clause=link["clause"], via=link["via"], kind="missing")
                )
        return AdminReview(items=items)

    return app
