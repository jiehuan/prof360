"""Export the current LLM insights of the golden companies to tests/golden/<ticker>.json.

Golden outputs (PLAN §7.3: MAR = hotels, JPM = financial institution, ENPH = technology) are the
regression baseline for prompt changes: rerun the insights stage after a prompt change, export
again, and review the diff (`git diff tests/golden`). Run from the repo root after
`uw-pipeline run --ticker MAR --ticker JPM --ticker ENPH --stage insights --stage metricdetail`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select, true

from uw_pipeline.clients.db import pipeline_engine
from uw_pipeline.schema import (
    company,
    followup_question,
    insight,
    insight_evidence,
    loan_portfolio_segment,
    metric_insight,
)
from uw_pipeline.settings import Settings

GOLDEN = ["MAR", "JPM", "ENPH"]
OUT = Path(__file__).resolve().parents[1] / "tests" / "golden"


def main() -> None:
    engine = pipeline_engine(Settings())
    OUT.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        for ticker in GOLDEN:
            company_id = conn.execute(
                select(company.c.company_id).where(company.c.ticker == ticker)
            ).scalar()
            if company_id is None:
                sys.exit(f"{ticker} not loaded")
            rows = conn.execute(
                select(insight)
                .where(insight.c.company_id == company_id, insight.c.is_current == true())
                .order_by(insight.c.kind, insight.c.ordinal)
            ).mappings()
            insights = []
            for r in rows:
                evidence = conn.execute(
                    select(insight_evidence.c.source_kind, insight_evidence.c.section_code,
                           insight_evidence.c.metric_code, insight_evidence.c.snippet,
                           insight_evidence.c.source_title)
                    .where(insight_evidence.c.insight_id == r["insight_id"])
                ).mappings()  # fmt: skip
                insights.append(
                    {k: r[k] for k in ("kind", "category", "subcategory", "insight_type",
                                       "title", "summary", "uw_relevance", "prompt_version")}
                    | {"evidence": [dict(e) for e in evidence]}
                )  # fmt: skip
            questions = [
                {k: q[k] for k in ("category", "question", "summary", "following_risk",
                                   "uw_relevance", "system_priority", "prompt_version")}
                for q in conn.execute(
                    select(followup_question)
                    .where(followup_question.c.company_id == company_id,
                           followup_question.c.is_current == true())
                    .order_by(followup_question.c.ordinal)
                ).mappings()
            ]  # fmt: skip
            metrics = {
                r.metric_code: r.uw_relevance
                for r in conn.execute(
                    select(metric_insight.c.metric_code, metric_insight.c.uw_relevance)
                    .where(metric_insight.c.company_id == company_id,
                           metric_insight.c.is_current == true())
                    .order_by(metric_insight.c.metric_code)
                )
            }  # fmt: skip
            lp = loan_portfolio_segment.c
            loans = [
                {k: r[k] for k in ("period_end", "parent_segment", "segment", "outstanding",
                                   "nonaccrual")}
                for r in conn.execute(
                    select(loan_portfolio_segment).where(lp.company_id == company_id)
                    .order_by(lp.period_end, lp.segment)
                ).mappings()
            ]  # fmt: skip
            path = OUT / f"{ticker}.json"
            doc = {"insights": insights, "followups": questions, "metric_relevance": metrics}
            if loans:
                doc["loan_portfolio"] = loans
            path.write_text(json.dumps(doc, indent=1) + "\n")
            print(f"wrote {path.relative_to(OUT.parents[1])}: {len(insights)} insights, "
                  f"{len(questions)} questions")  # fmt: skip


if __name__ == "__main__":
    main()
