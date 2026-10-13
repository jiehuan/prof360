"""Acceptance checks for LLM output (PLAN Phases 5 and 8): re-verify stored evidence against
sources, and check that every detail-modal object has its sources."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from sqlalchemy import Connection, func, select

from uw_pipeline.enrich.grounding import is_grounded
from uw_pipeline.schema import (
    activity,
    company,
    filing_section,
    followup_question,
    followup_question_source,
    insight,
    insight_evidence,
    loan_portfolio_segment,
    metric_detail,
    metric_insight,
    metric_value,
    signal,
)


@dataclass
class AuditReport:
    sampled_insights: int = 0
    snippets: int = 0
    snippets_found: int = 0
    insights_without_evidence: int = 0
    evidence_without_title: int = 0
    questions: int = 0
    questions_without_sources: int = 0
    loan_rows: int = 0
    loan_rows_found: int = 0
    metric_details: int = 0
    metric_relevance: int = 0
    fi_rows_for_non_fi: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.failures or self.insights_without_evidence or self.fi_rows_for_non_fi
                    or self.evidence_without_title or self.questions_without_sources)  # fmt: skip


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

    # Phase 8: every evidence row has a readable source, every question has sources, and the
    # loan book rows are found in the filing they cite.
    current_ids = [i.insight_id for i in current]
    rep.evidence_without_title = conn.execute(
        select(func.count()).where(insight_evidence.c.insight_id.in_(current_ids),
                                   insight_evidence.c.source_title.is_(None))
    ).scalar_one()  # fmt: skip
    fq, fs = followup_question.c, followup_question_source.c
    questions = conn.execute(
        select(fq.question_id, func.count(fs.source_row_id))
        .outerjoin(followup_question_source, fs.question_id == fq.question_id)
        .where(fq.is_current.is_(True))
        .group_by(fq.question_id)
    ).all()  # fmt: skip
    rep.questions = len(questions)
    rep.questions_without_sources = sum(1 for _, n in questions if not n)
    for r in conn.execute(select(loan_portfolio_segment)).mappings():
        rep.loan_rows += 1
        text = " ".join(conn.execute(select(filing_section.c.text).where(
            filing_section.c.doc_id == r["source_doc_id"])).scalars())  # fmt: skip
        if is_grounded(r["evidence"], text):
            rep.loan_rows_found += 1
        else:
            rep.failures.append(f"loan row {r['segment']} {r['period_end']}: {r['evidence'][:80]}")
    rep.metric_details = conn.execute(select(func.count()).select_from(metric_detail)).scalar_one()
    rep.metric_relevance = conn.execute(
        select(func.count()).where(metric_insight.c.is_current.is_(True))
    ).scalar_one()

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
