"""Company list and page: header, metrics, key insights, risks, follow-up questions, news,
FI modules and transcripts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from p360_bff import content
from p360_bff import schemas as s
from p360_bff import user_tables as ut
from p360_bff.deps import Conn, CurrentUser, company_or_404, now

router = APIRouter(prefix="/companies", tags=["companies"])


def summaries(conn: Any, rows: list[Any], user_id: int) -> list[s.CompanySummary]:
    n = content.counts(conn, [r["CompanyId"] for r in rows])
    fav = set(ut.company_set(conn, "favorites", user_id))
    pin = set(ut.company_set(conn, "pinned", user_id))
    return [content.summary(r, n[r["CompanyId"]], fav, pin) for r in rows]


@router.get("", response_model=list[s.CompanySummary])
def list_companies(conn: Conn, user: CurrentUser, search: str | None = None) -> Any:
    """Companies by name; `search` matches name or ticker. Counts for the dashboard cards."""
    return summaries(conn, content.company_rows(conn, search), user.user_id)


@router.get("/{company_id}", response_model=s.CompanyDetail)
def get_company(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Company header, counts and which modules have data. Records the view (Recently Viewed)."""
    row = company_or_404(conn, company_id)
    base = summaries(conn, [row], user.user_id)[0]
    ut.record_view(conn, user.user_id, company_id, now())
    return s.CompanyDetail(**base.model_dump(), modules=content.modules(conn, company_id))


@router.get("/{company_id}/metrics", response_model=s.MetricsResponse)
def get_metrics(
    company_id: int,
    conn: Conn,
    user: CurrentUser,
    view: Literal["quarterly", "annual"] = "quarterly",
) -> Any:
    """Metric cards by category. quarterly: the last 8 quarters (QoQ and YoY views); annual:
    the last 5 fiscal years (5Ys view). The user's metric choice for the industry applies."""
    row: Any = company_or_404(conn, company_id)
    chosen = ut.metric_selection(conn, user.user_id, row["SIC_Id"]).get(row["SIC_Id"])
    cats = content.metrics(conn, company_id, view, chosen, row["SIC_Id"])
    return s.MetricsResponse(view=view, customized=bool(chosen), categories=cats)


@router.get("/{company_id}/metrics/{metric_id}", response_model=s.MetricDetail)
def get_metric(company_id: int, metric_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Metric modal: methodology, data sources, data points, UW relevance and related items."""
    company_or_404(conn, company_id)
    detail = content.metric_detail(conn, company_id, metric_id)
    if detail is None:
        raise HTTPException(404, f"no data for metric {metric_id} at company {company_id}")
    return detail


@router.get("/{company_id}/key-insights", response_model=list[s.KeyInsight])
def get_key_insights(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    company_or_404(conn, company_id)
    return content.key_insights(conn, company_id)


@router.get("/{company_id}/key-insights/{insight_id}", response_model=s.KeyInsightDetail)
def get_key_insight(company_id: int, insight_id: int, conn: Conn, user: CurrentUser) -> Any:
    company_or_404(conn, company_id)
    item = content.key_insight(conn, company_id, insight_id)
    if item is None:
        raise HTTPException(404, f"key insight {insight_id} not found (or replaced)")
    return item


@router.get("/{company_id}/risks", response_model=list[s.RiskGroup])
def get_risks(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Strategic & Risk Insights in the page's four groups."""
    company_or_404(conn, company_id)
    return content.risks(conn, company_id)


@router.get("/{company_id}/risks/{risk_id}", response_model=s.RiskDetail)
def get_risk(company_id: int, risk_id: int, conn: Conn, user: CurrentUser) -> Any:
    company_or_404(conn, company_id)
    item = content.risk(conn, company_id, risk_id)
    if item is None:
        raise HTTPException(404, f"risk {risk_id} not found (or replaced)")
    return item


@router.get("/{company_id}/follow-ups", response_model=list[s.FollowUp])
def get_follow_ups(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    company_or_404(conn, company_id)
    return content.follow_ups(conn, company_id, ut.flagged_questions(conn, user.user_id))


@router.get("/{company_id}/follow-ups/{question_id}", response_model=s.FollowUpDetail)
def get_follow_up(company_id: int, question_id: int, conn: Conn, user: CurrentUser) -> Any:
    company_or_404(conn, company_id)
    item = content.follow_up(
        conn, company_id, question_id, ut.flagged_questions(conn, user.user_id)
    )
    if item is None:
        raise HTTPException(404, f"question {question_id} not found (or replaced)")
    return item


@router.put("/{company_id}/follow-ups/{question_id}/flag", response_model=s.FollowUp)
def flag_follow_up(
    company_id: int, question_id: int, body: s.FlagBody, conn: Conn, user: CurrentUser
) -> Any:
    """Flag or unflag a question for the current user. Only current questions can be flagged."""
    company_or_404(conn, company_id)
    if content.follow_up(conn, company_id, question_id, set()) is None:
        raise HTTPException(404, f"question {question_id} not found (or replaced)")
    ut.set_flag(conn, user.user_id, question_id, body.flagged, now())
    item = content.follow_up(
        conn, company_id, question_id, ut.flagged_questions(conn, user.user_id)
    )
    return item


@router.get("/{company_id}/activities", response_model=list[s.Activity])
def get_activities(
    company_id: int, conn: Conn, user: CurrentUser, days: int = Query(30, ge=1, le=365)
) -> Any:
    """Recent Activities (news), newest first, with the news risk score."""
    company_or_404(conn, company_id)
    return content.activities(conn, company_id, now() - timedelta(days=days))


@router.get("/{company_id}/trends", response_model=list[s.Trend])
def get_trends(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Performance Trends (financial institutions; empty for other companies)."""
    company_or_404(conn, company_id)
    return content.trends(conn, company_id)


@router.get("/{company_id}/loan-portfolio", response_model=s.LoanPortfolio)
def get_loan_portfolio(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Loan Portfolio Composition and its modal (financial institutions; empty otherwise)."""
    company_or_404(conn, company_id)
    return content.loan_portfolio(conn, company_id)


@router.get("/{company_id}/transcripts", response_model=list[s.TranscriptTone])
def get_transcripts(company_id: int, conn: Conn, user: CurrentUser) -> Any:
    """Earnings call tone and risk flags, latest quarter first."""
    company_or_404(conn, company_id)
    return content.transcripts(conn, company_id)
