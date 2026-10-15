"""Read queries over the application tables the pipeline publishes (schema_app.py). Rules from
docs/schema-contract.md: only isActive rows of the versioned tables; sources are JSON lists of
{title, url, date}; links between items come from ItemLink, read from either end."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import Connection, and_, func, or_, select, true

from p360_bff import schemas as s
from uw_pipeline.schema_app import app_metadata

T = app_metadata.tables
Company, SicCode = T["Company"], T["SIC_Code"]
Metrics, Category, SicPrefs = T["metrics"], T["metric_category"], T["SIC_Preferences"]
Matrix, MetricDetail = T["MatrixData"], T["CompanyMetricDetail"]
KeyInsights, KeyTypes = T["KeyInsights"], T["KeyInsightsTypes"]
Risks, RiskTypes = T["StrategicRisk"], T["RiskTypes"]
FollowUps, Links = T["FollowUpQuestions"], T["ItemLink"]
Activities, NewsRisk = T["Activities"], T["NewsRiskScore"]
Trend, TrendTypes = T["Trend"], T["TrendTypes"]
Loans, LoanTypes = T["LoanPortfolio"], T["LoanTypes"]
Tone, ToneFlags = T["TranscriptToneAnalysis"], T["TranscriptRiskFlag"]

RISK_CATEGORIES = {
    "governance": "Governance & Leadership",
    "risk": "Risk & Legal",
    "event": "Event Signals",
    "behavior": "Behavioral Insights",
}
SOURCE_LABELS = {
    "PRELIMINARY": "Preliminary (earnings release)",
    "LLM_EXTRACTED": "Extracted from filing text",
    "DERIVED": "Derived",
    "COMPUTED": "Computed",
}
# metric_category name -> page module key
MODULE_OF_CATEGORY = {
    "Financial": "financial",
    "Liquidity": "liquidity",
    "Leverage": "leverage",
    "Cash Flow": "cashFlow",
    "Operational Metrics": "operational",
    "Performance Trends": "performanceTrends",
    "Loan Portfolio": "loanPortfolio",
}
LOAN_METRIC = "Loan Portfolio Composition"
RISK_FLAG_TYPES = ("Warning", "Negative")
QUARTERS, YEARS = 8, 5


def sources(value: Any) -> list[s.Source]:
    return [
        s.Source(title=str(x.get("title") or ""), url=x.get("url"), date=x.get("date"))
        for x in s.json_list(value)
        if isinstance(x, dict)
    ]


def data_points(value: Any) -> list[s.DataPoint]:
    return [
        s.DataPoint(
            label=str(x.get("label") or ""),
            text=str(x.get("text") or ""),
            evidence=x.get("evidence"),
        )
        for x in s.json_list(value)
        if isinstance(x, dict)
    ]


def _lines(text: str | None) -> list[str]:
    return [ln for ln in (text or "").split("\n") if ln.strip()]


def _num(v: Any) -> float | None:
    return float(v) if v is not None else None


# -- companies ---------------------------------------------------------------------------------


def company_row(conn: Connection, company_id: int) -> Any:
    return (
        conn.execute(
            select(Company, SicCode.c.SIC_Code, SicCode.c.Description)
            .outerjoin(SicCode, SicCode.c.SIC_Id == Company.c.SIC_Id)
            .where(Company.c.CompanyId == company_id)
        )
        .mappings()
        .first()
    )


def company_rows(conn: Connection, search: str | None, ids: list[int] | None = None) -> list[Any]:
    q = select(Company, SicCode.c.SIC_Code, SicCode.c.Description).outerjoin(
        SicCode, SicCode.c.SIC_Id == Company.c.SIC_Id
    )
    if search:
        like = f"%{search.strip().lower()}%"
        q = q.where(
            or_(
                func.lower(Company.c.CompanyName).like(like),
                func.lower(Company.c.NameAbbreviation).like(like),
            )
        )
    if ids is not None:
        q = q.where(Company.c.CompanyId.in_(ids or [-1]))
    return list(conn.execute(q.order_by(Company.c.CompanyName)).mappings().all())


def counts(conn: Connection, company_ids: list[int]) -> dict[int, tuple[int, int]]:
    """(risk flags, active follow-up questions) per company."""
    flags: dict[int, int] = dict(
        conn.execute(
            select(KeyInsights.c.CompanyId, func.count())
            .join(KeyTypes, KeyTypes.c.KeyInsightsType_Id == KeyInsights.c.KeyInsightsType_Id)
            .where(
                KeyInsights.c.CompanyId.in_(company_ids or [-1]),
                KeyInsights.c.isActive == true(),
                KeyTypes.c.TypeName.in_(RISK_FLAG_TYPES),
            )
            .group_by(KeyInsights.c.CompanyId)
        ).all()
    )
    questions: dict[int, int] = dict(
        conn.execute(
            select(FollowUps.c.CompanyId, func.count())
            .where(FollowUps.c.CompanyId.in_(company_ids or [-1]), FollowUps.c.isActive == true())
            .group_by(FollowUps.c.CompanyId)
        ).all()
    )
    return {c: (flags.get(c, 0), questions.get(c, 0)) for c in company_ids}


def summary(
    row: Any, n: tuple[int, int], favorites: set[int], pinned: set[int]
) -> s.CompanySummary:
    cid = row["CompanyId"]
    sic = (
        s.Sic(sic_id=row["SIC_Id"], code=row["SIC_Code"], description=row["Description"])
        if row["SIC_Code"]
        else None
    )
    return s.CompanySummary(
        company_id=cid,
        name=row["CompanyName"],
        ticker=row["NameAbbreviation"],
        cik=row["CIK"],
        location=row["Location"],
        logo=row["logo"],
        sic=sic,
        updated_at=row["UpdatedAt"],
        risk_flags=n[0],
        follow_ups=n[1],
        is_favorite=cid in favorites,
        is_pinned=cid in pinned,
    )


def modules(conn: Connection, company_id: int) -> dict[str, bool]:
    present = {
        name
        for (name,) in conn.execute(
            select(Category.c.categoryName)
            .distinct()
            .join(Metrics, Metrics.c.metric_category_Id == Category.c.metric_category_Id)
            .join(Matrix, Matrix.c.metric_id == Metrics.c.metric_id)
            .where(Matrix.c.CompanyId == company_id)
        ).all()
    }
    out = {key: name in present for name, key in MODULE_OF_CATEGORY.items()}

    def any_row(table: Any, *where: Any) -> bool:
        return (
            conn.execute(
                select(table).where(table.c.CompanyId == company_id, *where).limit(1)
            ).first()
            is not None
        )

    out["performanceTrends"] = any_row(Trend)
    out["loanPortfolio"] = any_row(Loans)
    out["keyInsights"] = any_row(KeyInsights, KeyInsights.c.isActive == true())
    out["strategicRisks"] = any_row(Risks, Risks.c.isActive == true())
    out["followUps"] = any_row(FollowUps, FollowUps.c.isActive == true())
    out["activities"] = any_row(Activities)
    out["transcripts"] = any_row(Tone)
    return out


# -- metrics -----------------------------------------------------------------------------------


def _point(r: Any) -> s.MetricPoint:
    fp, fy = r["fiscal_period"], r["fiscal_year"]
    period = f"FY{fy}" if fp == "FY" else f"FY{fy} {fp}" if fy else str(r["period_end"])
    return s.MetricPoint(
        period=period,
        fiscal_year=fy,
        fiscal_period=fp,
        period_end=r["period_end"],
        value=r["value"] if r["status"] != "NM" else None,
        status=r["status"] or "OK",
        source_class=r["source_class"],
        source_label=SOURCE_LABELS.get(r["source_class"]),
        notes=r["notes"],
    )


def default_metric_ids(conn: Connection, sic_id: int | None) -> list[int]:
    return [
        r[0]
        for r in conn.execute(select(SicPrefs.c.metric_id).where(SicPrefs.c.SIC_Id == sic_id)).all()
    ]


def metric_refs(conn: Connection, first: list[int]) -> list[s.MetricRef]:
    """Every metric except the loan-portfolio pseudo metric; the ids in `first` lead."""
    rows = conn.execute(
        select(Metrics.c.metric_id, Metrics.c.metric_name, Category.c.categoryName)
        .outerjoin(Category, Category.c.metric_category_Id == Metrics.c.metric_category_Id)
        .where(Metrics.c.metric_name != LOAN_METRIC)
        .order_by(Metrics.c.metric_id)
    ).all()
    lead = set(first)
    refs = [s.MetricRef(metric_id=r[0], name=r[1], category=r[2]) for r in rows]
    return [m for m in refs if m.metric_id in lead] + [m for m in refs if m.metric_id not in lead]


def metrics(
    conn: Connection, company_id: int, view: str, chosen: list[int] | None, sic_id: int | None
) -> list[s.MetricCategory]:
    """Metric cards by category: the user's choice for this industry, else the industry's
    default set (SIC_Preferences), else every metric with data. Only metrics with values."""
    wanted = chosen or default_metric_ids(conn, sic_id) or None
    quarterly = view == "quarterly"
    q = (
        select(
            Matrix,
            Metrics.c.metric_name,
            Metrics.c.subtitle,
            Metrics.c.description,
            Category.c.categoryName,
            Category.c.metric_category_Id,
        )
        .join(Metrics, Metrics.c.metric_id == Matrix.c.metric_id)
        .outerjoin(Category, Category.c.metric_category_Id == Metrics.c.metric_category_Id)
        .where(
            Matrix.c.CompanyId == company_id,
            Matrix.c.fiscal_period.like("Q%") if quarterly else Matrix.c.fiscal_period == "FY",
        )
        .order_by(Matrix.c.period_end)
    )
    if wanted is not None:
        q = q.where(Matrix.c.metric_id.in_(wanted))
    series: dict[int, list[Any]] = defaultdict(list)
    meta: dict[int, Any] = {}
    for r in conn.execute(q).mappings().all():
        series[r["metric_id"]].append(r)
        meta[r["metric_id"]] = r
    keep = QUARTERS if quarterly else YEARS
    by_cat: dict[tuple[int, str], list[s.Metric]] = defaultdict(list)
    for mid, rows in series.items():
        rows = rows[-keep:]
        m = meta[mid]
        points = [_point(r) for r in rows]
        by_cat[(m["metric_category_Id"] or 0, m["categoryName"] or "Other")].append(
            s.Metric(
                metric_id=mid,
                name=m["metric_name"],
                subtitle=m["subtitle"],
                description=m["description"],
                unit=rows[-1]["unit"],
                latest=points[-1],
                series=points,
            )
        )
    return [
        s.MetricCategory(category=name, metrics=sorted(ms, key=lambda x: x.metric_id))
        for (_, name), ms in sorted(by_cat.items())
    ]


def metric_detail(conn: Connection, company_id: int, metric_id: int) -> s.MetricDetail | None:
    m = (
        conn.execute(
            select(Metrics, Category.c.categoryName)
            .outerjoin(Category, Category.c.metric_category_Id == Metrics.c.metric_category_Id)
            .where(Metrics.c.metric_id == metric_id)
        )
        .mappings()
        .first()
    )
    d = (
        conn.execute(
            select(MetricDetail).where(
                MetricDetail.c.CompanyId == company_id, MetricDetail.c.metric_id == metric_id
            )
        )
        .mappings()
        .first()
    )
    has_values = conn.execute(
        select(Matrix.c.MatrixDataId)
        .where(Matrix.c.CompanyId == company_id, Matrix.c.metric_id == metric_id)
        .limit(1)
    ).first()
    if m is None or (d is None and has_values is None):
        return None
    return s.MetricDetail(
        metric_id=metric_id,
        name=m["metric_name"],
        category=m["categoryName"],
        formula=m["Formula"],
        methodology=_lines(d["Methodology"] if d else None),
        data_sources=sources(d["DataSources"] if d else None),
        data_points=data_points(d["DataPoints"] if d else None),
        underwriting_relevance=d["UnderwritingRelevance"] if d else None,
        updated_at=d["UpdatedAt"] if d else None,
        related=related(conn, company_id, "METRIC", metric_id),
    )


# -- insights, risks, follow-up questions ------------------------------------------------------


def _key_insight(r: Any) -> s.KeyInsight:
    return s.KeyInsight(
        id=r["UniqueID"],
        type=r["TypeName"],
        icon_type=r["iconType"],
        title=r["title"],
        summary=r["summary"],
        underwriting=r["underwriting"],
        evidence=r["evidence"],
        sources=sources(r["source"]),
        updated_date=r["updatedDate"],
        link=r["link"],
        confidence=_num(r["confidence"]),
    )


def _key_query() -> Any:
    return (
        select(KeyInsights, KeyTypes.c.TypeName)
        .outerjoin(KeyTypes, KeyTypes.c.KeyInsightsType_Id == KeyInsights.c.KeyInsightsType_Id)
        .where(KeyInsights.c.isActive == true())
    )


def key_insights(conn: Connection, company_id: int) -> list[s.KeyInsight]:
    rows = (
        conn.execute(
            _key_query()
            .where(KeyInsights.c.CompanyId == company_id)
            .order_by(KeyInsights.c.UniqueID)
        )
        .mappings()
        .all()
    )
    return [_key_insight(r) for r in rows]


def key_insight(conn: Connection, company_id: int, insight_id: int) -> s.KeyInsightDetail | None:
    r = (
        conn.execute(
            _key_query().where(
                KeyInsights.c.CompanyId == company_id, KeyInsights.c.UniqueID == insight_id
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    return s.KeyInsightDetail(
        **_key_insight(r).model_dump(), related=related(conn, company_id, "KEY_INSIGHT", insight_id)
    )


def _risk(r: Any) -> s.Risk:
    cat = r["Category"] or ""
    return s.Risk(
        id=r["StrategicRiskId"],
        category=cat,
        category_label=RISK_CATEGORIES.get(cat, cat),
        risk_type=r["RiskTypeName"],
        updated_at=r["UpdatedAt"],
        title=r["Title"],
        summary=r["Summary"],
        underwriting_relevance=r["UnderwritingRelevance"],
        supporting_evidence=r["SupportingEvidence"],
        sources=sources(r["sources"]),
    )


def _risk_query() -> Any:
    return (
        select(Risks, RiskTypes.c.RiskTypeName)
        .outerjoin(RiskTypes, RiskTypes.c.RiskType_Id == Risks.c.RiskType_Id)
        .where(Risks.c.isActive == true())
    )


def risks(conn: Connection, company_id: int) -> list[s.RiskGroup]:
    rows = (
        conn.execute(
            _risk_query().where(Risks.c.CompanyId == company_id).order_by(Risks.c.StrategicRiskId)
        )
        .mappings()
        .all()
    )
    groups: dict[str, list[s.Risk]] = defaultdict(list)
    for r in rows:
        groups[r["Category"] or ""].append(_risk(r))
    order = [*RISK_CATEGORIES, *sorted(set(groups) - set(RISK_CATEGORIES))]
    return [
        s.RiskGroup(category=c, category_label=RISK_CATEGORIES.get(c, c), risks=groups[c])
        for c in order
        if groups.get(c)
    ]


def risk(conn: Connection, company_id: int, risk_id: int) -> s.RiskDetail | None:
    r = (
        conn.execute(
            _risk_query().where(Risks.c.CompanyId == company_id, Risks.c.StrategicRiskId == risk_id)
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    return s.RiskDetail(
        **_risk(r).model_dump(), related=related(conn, company_id, "STRATEGIC_RISK", risk_id)
    )


def _follow_up(r: Any, flagged: set[int]) -> s.FollowUp:
    return s.FollowUp(
        id=r["FollowUpQuestionId"],
        category=r["category"],
        question=r["title"],
        summary=r["summary"],
        following_risk=r["followingRisk"],
        underwriting_relevance=r["underwritingRelevance"],
        priority=r["Priority"],
        sources=sources(r["sources"]),
        is_flagged=r["FollowUpQuestionId"] in flagged,
    )


def follow_ups(conn: Connection, company_id: int, flagged: set[int]) -> list[s.FollowUp]:
    rows = (
        conn.execute(
            select(FollowUps)
            .where(FollowUps.c.CompanyId == company_id, FollowUps.c.isActive == true())
            .order_by(FollowUps.c.FollowUpQuestionId)
        )
        .mappings()
        .all()
    )
    return [_follow_up(r, flagged) for r in rows]


def follow_up(
    conn: Connection, company_id: int, question_id: int, flagged: set[int]
) -> s.FollowUpDetail | None:
    r = (
        conn.execute(
            select(FollowUps).where(
                FollowUps.c.CompanyId == company_id,
                FollowUps.c.FollowUpQuestionId == question_id,
                FollowUps.c.isActive == true(),
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    return s.FollowUpDetail(
        **_follow_up(r, flagged).model_dump(),
        related=related(conn, company_id, "FOLLOWUP", question_id),
    )


def related(conn: Connection, company_id: int, kind: str, item_id: int) -> s.Related:
    """Items linked to this one in ItemLink (either direction); only active ones."""
    rows = (
        conn.execute(
            select(Links).where(
                Links.c.CompanyId == company_id,
                or_(
                    and_(Links.c.FromType == kind, Links.c.FromId == item_id),
                    and_(Links.c.ToType == kind, Links.c.ToId == item_id),
                ),
            )
        )
        .mappings()
        .all()
    )
    other: dict[str, set[int]] = defaultdict(set)
    for r in rows:
        if r["FromType"] == kind and r["FromId"] == item_id:
            other[r["ToType"]].add(r["ToId"])
        else:
            other[r["FromType"]].add(r["FromId"])
    out = s.Related()
    if other["KEY_INSIGHT"]:
        rows = (
            conn.execute(
                _key_query()
                .where(KeyInsights.c.UniqueID.in_(other["KEY_INSIGHT"]))
                .order_by(KeyInsights.c.UniqueID)
            )
            .mappings()
            .all()
        )
        out.key_insights = [
            s.KeyInsightRef(
                id=r["UniqueID"], type=r["TypeName"], title=r["title"], summary=r["summary"]
            )
            for r in rows
        ]
    if other["STRATEGIC_RISK"]:
        rows = (
            conn.execute(
                _risk_query()
                .where(Risks.c.StrategicRiskId.in_(other["STRATEGIC_RISK"]))
                .order_by(Risks.c.StrategicRiskId)
            )
            .mappings()
            .all()
        )
        out.risks = [
            s.RiskRef(
                id=r["StrategicRiskId"],
                category=r["Category"] or "",
                category_label=RISK_CATEGORIES.get(r["Category"] or "", ""),
                risk_type=r["RiskTypeName"],
                title=r["Title"],
            )
            for r in rows
        ]
    if other["FOLLOWUP"]:
        rows = (
            conn.execute(
                select(FollowUps)
                .where(
                    FollowUps.c.FollowUpQuestionId.in_(other["FOLLOWUP"]),
                    FollowUps.c.isActive == true(),
                )
                .order_by(FollowUps.c.FollowUpQuestionId)
            )
            .mappings()
            .all()
        )
        out.follow_ups = [
            s.FollowUpRef(
                id=r["FollowUpQuestionId"],
                category=r["category"],
                question=r["title"],
                priority=r["Priority"],
            )
            for r in rows
        ]
    if other["METRIC"]:
        with_data = {
            r[0]
            for r in conn.execute(
                select(Matrix.c.metric_id)
                .distinct()
                .where(Matrix.c.CompanyId == company_id, Matrix.c.metric_id.in_(other["METRIC"]))
            ).all()
        }
        metric_rows = conn.execute(
            select(Metrics.c.metric_id, Metrics.c.metric_name, Category.c.categoryName)
            .outerjoin(Category, Category.c.metric_category_Id == Metrics.c.metric_category_Id)
            .where(Metrics.c.metric_id.in_(with_data or {-1}))
            .order_by(Metrics.c.metric_id)
        ).all()
        out.metrics = [s.MetricRef(metric_id=r[0], name=r[1], category=r[2]) for r in metric_rows]
    return out


# -- activities, FI modules, transcripts -------------------------------------------------------


def activities(conn: Connection, company_id: int, since: Any) -> list[s.Activity]:
    rows = (
        conn.execute(
            select(Activities)
            .where(Activities.c.CompanyId == company_id, Activities.c.updateDate >= since)
            .order_by(Activities.c.updateDate.desc())
        )
        .mappings()
        .all()
    )
    scores: dict[int, Any] = {}
    ids = [r["ActivityId"] for r in rows]
    for r in (
        conn.execute(
            select(NewsRisk)
            .where(NewsRisk.c.ActivityId.in_(ids or [-1]))
            .order_by(NewsRisk.c.createdAt)
        )
        .mappings()
        .all()
    ):
        scores[r["ActivityId"]] = r  # the latest run wins
    out = []
    for r in rows:
        sc = scores.get(r["ActivityId"])
        score = (
            s.RiskScore(
                overall=_num(sc["overallRiskScore"]),
                litigation=_num(sc["litigationEO"]),
                regulatory=_num(sc["regulatory"]),
                financial_distress=_num(sc["financialDistress"]),
                governance=_num(sc["governance"]),
                operational=_num(sc["operational"]),
                rationale=sc["rationale"],
            )
            if sc is not None
            else None
        )
        out.append(
            s.Activity(
                id=r["ActivityId"],
                title=r["title"],
                date=r["updateDate"],
                summary=r["summary"],
                sentiment=r["sentiment"],
                underwriting=r["underwriting"],
                sources=sources(r["sources"]),
                confidence=_num(r["confidence"]),
                risk_score=score,
            )
        )
    return out


def trends(conn: Connection, company_id: int) -> list[s.Trend]:
    rows = (
        conn.execute(
            select(Trend, TrendTypes.c.TrendName, TrendTypes.c.Methodology)
            .join(TrendTypes, TrendTypes.c.TrendTypeId == Trend.c.TrendTypeId)
            .where(Trend.c.CompanyId == company_id)
            .order_by(Trend.c.TrendTypeId, Trend.c.Year)
        )
        .mappings()
        .all()
    )
    out: dict[int, s.Trend] = {}
    for r in rows:
        t = out.setdefault(
            r["TrendTypeId"],
            s.Trend(
                trend_type_id=r["TrendTypeId"],
                name=r["TrendName"],
                methodology=r["Methodology"],
                points=[],
            ),
        )
        t.points.append(
            s.TrendPoint(
                year=r["Year"],
                value=r["Value"],
                data_sources=sources(r["DataSources"]),
                data_points=data_points(r["DataPoints"]),
            )
        )
    return list(out.values())


def loan_portfolio(conn: Connection, company_id: int) -> s.LoanPortfolio:
    rows = (
        conn.execute(
            select(Loans, LoanTypes.c.LoanTypeName, LoanTypes.c.ParentSegment)
            .join(LoanTypes, LoanTypes.c.LoanTypeId == Loans.c.LoanTypeId)
            .where(Loans.c.CompanyId == company_id)
            .order_by(Loans.c.PeriodEnd.desc(), LoanTypes.c.ParentSegment, LoanTypes.c.LoanTypeName)
        )
        .mappings()
        .all()
    )
    periods = sorted({r["PeriodEnd"] for r in rows if r["PeriodEnd"]}, reverse=True)
    d = (
        conn.execute(
            select(MetricDetail)
            .join(Metrics, Metrics.c.metric_id == MetricDetail.c.metric_id)
            .where(MetricDetail.c.CompanyId == company_id, Metrics.c.metric_name == LOAN_METRIC)
        )
        .mappings()
        .first()
    )
    return s.LoanPortfolio(
        current_period=periods[0] if periods else None,
        prior_period=periods[1] if len(periods) > 1 else None,
        segments=[
            s.LoanSegment(
                parent_segment=r["ParentSegment"],
                segment=r["LoanTypeName"],
                period_end=r["PeriodEnd"],
                value=_num(r["LoanValue"]),
                pct_of_book=_num(r["PctOfBook"]),
                nonaccrual=_num(r["Nonaccrual"]),
                nonaccrual_ratio=_num(r["NonaccrualRatio"]),
            )
            for r in rows
        ],
        methodology=_lines(d["Methodology"] if d else None),
        data_sources=sources(d["DataSources"] if d else None),
        data_points=data_points(d["DataPoints"] if d else None),
    )


def transcripts(conn: Connection, company_id: int) -> list[s.TranscriptTone]:
    """The latest analysis run of each transcript, newest quarter first. The model version
    column is not exposed (text shown to users does not name models)."""
    rows = (
        conn.execute(
            select(Tone)
            .where(Tone.c.CompanyId == company_id)
            .order_by(Tone.c.TranscriptsId, Tone.c.runId)
        )
        .mappings()
        .all()
    )
    latest: dict[int, Any] = {}
    for r in rows:
        latest[r["TranscriptsId"]] = r
    flags: dict[tuple[int, int], list[s.TranscriptFlag]] = defaultdict(list)
    for f in (
        conn.execute(
            select(ToneFlags)
            .where(ToneFlags.c.TranscriptsId.in_(list(latest) or [-1]))
            .order_by(ToneFlags.c.FlagIndex)
        )
        .mappings()
        .all()
    ):
        flags[(f["TranscriptsId"], f["runId"])].append(
            s.TranscriptFlag(index=f["FlagIndex"], category=f["Category"], text=f["FlagText"])
        )
    out = [
        s.TranscriptTone(
            transcript_id=r["TranscriptsId"],
            quarter=r["Quarter"],
            overall_tone=_num(r["overallTone"]),
            confidence=_num(r["confidence"]),
            hedging=_num(r["hedging"]),
            uncertainty=_num(r["uncertainty"]),
            forward_looking=_num(r["forwardLooking"]),
            liability_cues=_num(r["liabilityCues"]),
            chunks=r["nChunks"],
            analyzed_at=r["createdAt"],
            flags=flags.get((r["TranscriptsId"], r["runId"]), []),
        )
        for r in latest.values()
    ]
    return sorted(out, key=lambda t: t.quarter or "", reverse=True)
