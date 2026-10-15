"""Response and request bodies. JSON field names are camelCase for the front end."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Source(Model):
    """A document behind a value or an insight. Transcripts have no public url."""

    title: str
    url: str | None = None
    date: str | None = None


class DataPoint(Model):
    label: str
    text: str
    evidence: str | None = None


class Sic(Model):
    sic_id: int
    code: str
    description: str


class CompanySummary(Model):
    company_id: int
    name: str
    ticker: str
    cik: str
    location: str | None
    logo: str | None
    sic: Sic | None
    updated_at: date | None
    risk_flags: int  # active key insights of type Warning or Negative (D-048)
    follow_ups: int
    is_favorite: bool
    is_pinned: bool


class CompanyDetail(CompanySummary):
    # Which page modules have data; the front end hides the rest (e.g. no Loan Portfolio for a
    # hotel group). Keys: financial, liquidity, leverage, cashFlow, operational,
    # performanceTrends, loanPortfolio, keyInsights, strategicRisks, followUps, activities,
    # transcripts.
    modules: dict[str, bool]


class MetricPoint(Model):
    period: str  # "FY2026 Q2" or "FY2025", the company's own fiscal calendar
    fiscal_year: int | None
    fiscal_period: str | None  # Q1..Q4 or FY
    period_end: date | None
    value: float | None  # None when status is NM
    status: str  # OK | NM (not meaningful, e.g. D/E with negative equity)
    source_class: str  # REPORTED | DERIVED | COMPUTED | LLM_EXTRACTED | PRELIMINARY
    source_label: str | None  # e.g. "Preliminary (earnings release)"; None for reported values
    notes: str | None


class Metric(Model):
    metric_id: int
    name: str
    subtitle: str | None
    description: str | None
    unit: str | None  # USD | ratio | fraction | ...
    latest: MetricPoint
    series: list[MetricPoint]  # oldest first


class MetricCategory(Model):
    category: str
    metrics: list[Metric]


class MetricsResponse(Model):
    view: str  # quarterly | annual
    customized: bool  # the user's own metric selection for this industry
    categories: list[MetricCategory]


class KeyInsightRef(Model):
    id: int
    type: str | None
    title: str
    summary: str | None


class RiskRef(Model):
    id: int
    category: str
    category_label: str
    risk_type: str | None
    title: str


class FollowUpRef(Model):
    id: int
    category: str | None
    question: str
    priority: str | None


class MetricRef(Model):
    metric_id: int
    name: str
    category: str | None


class Related(Model):
    key_insights: list[KeyInsightRef] = []
    risks: list[RiskRef] = []
    follow_ups: list[FollowUpRef] = []
    metrics: list[MetricRef] = []


class MetricDetail(Model):
    metric_id: int
    name: str
    category: str | None
    formula: str | None
    methodology: list[str]  # lines
    data_sources: list[Source]
    data_points: list[DataPoint]
    underwriting_relevance: str | None
    updated_at: datetime | None
    related: Related


class KeyInsight(Model):
    id: int
    type: str | None  # Warning | Negative | Positive | Info
    icon_type: str | None
    title: str
    summary: str | None
    underwriting: str | None
    evidence: str | None
    sources: list[Source]
    updated_date: date | None
    link: str | None
    confidence: float | None


class KeyInsightDetail(KeyInsight):
    related: Related


class Risk(Model):
    id: int
    category: str  # governance | risk | event | behavior
    category_label: (
        str  # Governance & Leadership | Risk & Legal | Event Signals | Behavioral Insights
    )
    risk_type: str | None
    updated_at: date | None
    title: str
    summary: str | None
    underwriting_relevance: str | None
    supporting_evidence: str | None
    sources: list[Source]


class RiskDetail(Risk):
    related: Related


class RiskGroup(Model):
    category: str
    category_label: str
    risks: list[Risk]


class FollowUp(Model):
    id: int
    category: str | None
    question: str
    summary: str | None
    following_risk: str | None
    underwriting_relevance: str | None
    priority: str | None
    sources: list[Source]
    is_flagged: bool


class FollowUpDetail(FollowUp):
    related: Related


class FlagBody(Model):
    flagged: bool


class RiskScore(Model):
    overall: float | None
    litigation: float | None
    regulatory: float | None
    financial_distress: float | None
    governance: float | None
    operational: float | None
    rationale: str | None


class Activity(Model):
    id: int
    title: str
    date: datetime | None
    summary: str | None
    sentiment: str | None
    underwriting: str | None
    sources: list[Source]
    confidence: float | None
    risk_score: RiskScore | None


class TrendPoint(Model):
    year: int
    value: float | None
    data_sources: list[Source]
    data_points: list[DataPoint]


class Trend(Model):
    trend_type_id: int
    name: str
    methodology: str | None
    points: list[TrendPoint]  # oldest first


class LoanSegment(Model):
    parent_segment: str | None
    segment: str
    period_end: date | None
    value: float | None
    pct_of_book: float | None
    nonaccrual: float | None
    nonaccrual_ratio: float | None


class LoanPortfolio(Model):
    current_period: date | None
    prior_period: date | None
    segments: list[LoanSegment]
    methodology: list[str]
    data_sources: list[Source]
    data_points: list[DataPoint]


class TranscriptFlag(Model):
    index: int
    category: str | None
    text: str


class TranscriptTone(Model):
    transcript_id: int
    quarter: str | None
    overall_tone: float | None
    confidence: float | None
    hedging: float | None
    uncertainty: float | None
    forward_looking: float | None
    liability_cues: float | None
    chunks: int | None
    analyzed_at: datetime | None
    flags: list[TranscriptFlag]


class Me(Model):
    user_id: int
    email: str
    display_name: str | None


class RecentCompany(Model):
    company: CompanySummary
    viewed_at: datetime


class Preferences(Model):
    followed_sic_ids: list[int]
    metric_selections: dict[int, list[int]]  # SIC id -> chosen metric ids


class IdList(Model):
    ids: list[int]


def json_list(value: Any) -> list[Any]:
    """A JSON list column as a list; empty for NULL or unreadable text."""
    if not value:
        return []
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []
