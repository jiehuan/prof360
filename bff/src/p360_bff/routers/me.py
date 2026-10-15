"""The current user: favorites, pinned companies, recently viewed, preferences."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Response

from p360_bff import content
from p360_bff import schemas as s
from p360_bff import user_tables as ut
from p360_bff.deps import Conn, CurrentUser, company_or_404, now
from p360_bff.routers.companies import summaries

router = APIRouter(prefix="/me", tags=["me"])
Kind = Literal["favorites", "pinned"]


@router.get("", response_model=s.Me)
def me(user: CurrentUser) -> Any:
    return s.Me(user_id=user.user_id, email=user.email, display_name=user.display_name)


@router.get("/recent", response_model=list[s.RecentCompany])
def recent(conn: Conn, user: CurrentUser, limit: int = Query(10, ge=1, le=50)) -> Any:
    views = ut.recent(conn, user.user_id, limit)
    rows = {r["CompanyId"]: r for r in content.company_rows(conn, None, [c for c, _ in views])}
    by_id = {c.company_id: c for c in summaries(conn, list(rows.values()), user.user_id)}
    return [s.RecentCompany(company=by_id[c], viewed_at=at) for c, at in views if c in by_id]


@router.get("/preferences", response_model=s.Preferences)
def get_preferences(conn: Conn, user: CurrentUser) -> Any:
    return s.Preferences(
        followed_sic_ids=ut.followed_sics(conn, user.user_id),
        metric_selections=ut.metric_selection(conn, user.user_id),
    )


@router.put("/preferences/industries", response_model=s.Preferences)
def set_industries(body: s.IdList, conn: Conn, user: CurrentUser) -> Any:
    """Industries (SIC ids) the user follows; replaces the list."""
    ut.set_followed_sics(conn, user.user_id, body.ids)
    return get_preferences(conn, user)


@router.put("/preferences/metrics/{sic_id}", response_model=s.Preferences)
def set_metrics(sic_id: int, body: s.IdList, conn: Conn, user: CurrentUser) -> Any:
    """Metrics shown for companies of this industry; an empty list restores the default."""
    ut.set_metric_selection(conn, user.user_id, sic_id, body.ids)
    return get_preferences(conn, user)


@router.get("/preferences/metrics/{sic_id}/available", response_model=list[s.MetricRef])
def available_metrics(sic_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Metrics that can be chosen for an industry: the industry's default set first."""
    default = content.default_metric_ids(conn, sic_id)
    return content.metric_refs(conn, default)


@router.get("/{kind}", response_model=list[s.CompanySummary])
def company_set(kind: Kind, conn: Conn, user: CurrentUser) -> Any:
    """Favorite or pinned companies."""
    ids = ut.company_set(conn, kind, user.user_id)
    by_id = {r["CompanyId"]: r for r in content.company_rows(conn, None, ids)}
    return summaries(conn, [by_id[i] for i in ids if i in by_id], user.user_id)


@router.put("/{kind}/{company_id}", status_code=204)
def add_company(kind: Kind, company_id: int, conn: Conn, user: CurrentUser) -> Response:
    company_or_404(conn, company_id)
    ut.set_company(conn, kind, user.user_id, company_id, True, now())
    return Response(status_code=204)


@router.delete("/{kind}/{company_id}", status_code=204)
def remove_company(kind: Kind, company_id: int, conn: Conn, user: CurrentUser) -> Response:
    ut.set_company(conn, kind, user.user_id, company_id, False, now())
    return Response(status_code=204)
