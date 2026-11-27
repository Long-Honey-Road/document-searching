"""HTTP-граница заявок и аналитики ОКК."""

from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from regagent_backend.identity import AuthError, UserIdentity, identity_from_auth_user
from regagent_backend.qc_analytics import Analytics, AnalyticsFilters, AnalyticsReport
from regagent_backend.qc_models import (
    CreateTicket,
    PublishDraft,
    ReviewSummary,
    TicketDetail,
    TicketStatus,
    TicketSummary,
)
from regagent_backend.qc_store import ADMIN_ROLE, Conflict, Forbidden, NotFound, QCStore
from regagent_backend.settings import BackendSettings


async def _errors() -> AsyncIterator[None]:
    try:
        yield
    except Forbidden as exc:
        raise HTTPException(403, "forbidden") from exc
    except NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except Conflict as exc:
        raise HTTPException(409, "ticket state changed") from exc


router = APIRouter(prefix="/qc", dependencies=[Depends(_errors)])


async def _identity(request: Request) -> UserIdentity:
    user = request.scope.get("user")
    if not getattr(user, "is_authenticated", False):
        raise HTTPException(401, "unauthorized")
    try:
        return identity_from_auth_user(user)
    except AuthError as exc:
        raise HTTPException(401, "unauthorized") from exc


def _store(request: Request) -> QCStore:
    return cast(QCStore, request.app.state.qc_store)


def _analytics(request: Request) -> Analytics:
    return cast(Analytics, request.app.state.qc_analytics)


Identity = Annotated[UserIdentity, Depends(_identity)]
Store = Annotated[QCStore, Depends(_store)]
Report = Annotated[Analytics, Depends(_analytics)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2_147_483_647)]


@router.get("/me")
async def capabilities(identity: Identity) -> dict[str, bool | list[str]]:
    can_review = ADMIN_ROLE in identity.roles and bool(identity.levels)
    return {"can_review": can_review, "areas": BackendSettings().AREA_CODES if can_review else []}


@router.post("/tickets", status_code=201)
async def create_ticket(payload: CreateTicket, identity: Identity, store: Store) -> TicketSummary:
    return await store.create(identity, payload)


@router.get("/tickets")
async def own_tickets(
    identity: Identity,
    store: Store,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[TicketSummary]:
    return await store.mine(identity, limit=limit, offset=offset)


@router.get("/admin/tickets")
async def review_queue(
    identity: Identity,
    store: Store,
    status: TicketStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[ReviewSummary]:
    return await store.review_queue(identity, status=status, limit=limit, offset=offset)


@router.get("/admin/tickets/{ticket_id}")
async def review_ticket(ticket_id: UUID, identity: Identity, store: Store) -> TicketDetail:
    return await store.review(identity, ticket_id)


@router.post("/admin/tickets/{ticket_id}/publish")
async def publish_ticket(
    ticket_id: UUID,
    draft: PublishDraft,
    identity: Identity,
    store: Store,
) -> TicketDetail:
    return await store.publish(identity, ticket_id, draft)


@router.post("/admin/tickets/{ticket_id}/reject")
async def reject_ticket(ticket_id: UUID, identity: Identity, store: Store) -> TicketDetail:
    return await store.reject(identity, ticket_id)


@router.get("/admin/analytics")
async def analytics_report(
    filters: Annotated[AnalyticsFilters, Query()],
    identity: Identity,
    analytics: Report,
) -> AnalyticsReport:
    return await analytics.report(identity, filters)


@router.post("/admin/analytics/{run_id}/ticket", status_code=201)
async def ticket_from_analytics(
    run_id: UUID,
    identity: Identity,
    analytics: Report,
) -> TicketSummary:
    return await analytics.create_ticket(identity, run_id)
