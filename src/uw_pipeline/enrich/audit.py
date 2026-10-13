"""Acceptance checks for LLM output (PLAN Phase 5): re-verify stored evidence against sources."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from sqlalchemy import Connection, func, select

from uw_pipeline.enrich.grounding import is_grounded
from uw_pipeline.schema import (
    activity,
    company,
    filing_section,
    insight,
    insight_evidence,
    loan_portfolio_segment,
    metric_value,
    signal,
)


@dataclass
class AuditReport:
    sampled_insights: int = 0
    snippets: int = 0
    snippets_found: int = 0
    insights_without_evidence: int = 0
    fi_rows_for_non_fi: int = 0
    failures: list[str] = field(default_factory=list)


def _source_text(conn: Connection, ev: dict[str, object]) -> str:
    kind = ev["source_kind"]
    if kind == "DOCUMENT" and ev["doc_id"] is not None:
        return " ".join(
            conn.execute(
                select(filing_section.c.text).where(filing_section.c.doc_id == ev["doc_id"])
            ).scalars()
        )
    if kind == "SIGNAL" and ev["signal_id"] is not None:
        row = conn.execute(
            select(signal.c.title, signal.c.snippet, signal.c.description, signal.c.event_date,
                   signal.c.severity).where(signal.c.signal_id == ev["signal_id"])
        ).first()  # fmt: skip
        return " | ".join(str(x) for x in row) if row else ""
    if kind == "NEWS" and ev["activity_id"] is not None:
        news = conn.execute(
            select(activity.c.title, activity.c.av_summary, activity.c.summary).where(
                activity.c.activity_id == ev["activity_id"]
            )
        ).first()
        return " | ".join(str(x) for x in news) if news else ""
    return ""


def audit(conn: Connection, sample: int = 20, seed: int = 7) -> AuditReport:
    rep = AuditReport()
    current = conn.execute(
        select(insight.c.insight_id, insight.c.title, insight.c.subcategory).where(
            insight.c.is_current.is_(True)
        )
    ).all()
    with_evidence = []
    for i in current:
        n = conn.execute(
            select(func.count()).where(insight_evidence.c.insight_id == i.insight_id)
        ).scalar_one()
        if n == 0 and i.subcategory != "No signal":
            rep.insights_without_evidence += 1
        elif n:
            with_evidence.append(i)
    picked = random.Random(seed).sample(with_evidence, min(sample, len(with_evidence)))
    rep.sampled_insights = len(picked)
    for i in picked:
        for ev in conn.execute(
            select(insight_evidence).where(insight_evidence.c.insight_id == i.insight_id)
        ).mappings():
            if ev["source_kind"] == "METRIC":
                continue  # metric rows are rendered from metric_value; checked at generation
            rep.snippets += 1
            if is_grounded(ev["snippet"], _source_text(conn, dict(ev)), min_chars=4):
                rep.snippets_found += 1
            else:
                rep.failures.append(f"insight {i.insight_id} '{i.title}': {ev['snippet'][:80]}")

    non_fi = select(company.c.company_id).where(company.c.is_financial.is_(False))
    fi_codes = ("cet1_ratio", "tier1_ratio", "lcr", "nim", "npl_ratio")
    rep.fi_rows_for_non_fi = (
        conn.execute(
            select(func.count()).where(
                metric_value.c.company_id.in_(non_fi), metric_value.c.metric_code.in_(fi_codes)
            )
        ).scalar_one()
        + conn.execute(
            select(func.count()).where(loan_portfolio_segment.c.company_id.in_(non_fi))
        ).scalar_one()
    )
    return rep
