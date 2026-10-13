"""Export the current LLM insights of the golden companies to tests/golden/<ticker>.json.

Golden outputs (PLAN §7.3: MAR = hotels, JPM = financial institution, ENPH = technology) are the
regression baseline for prompt changes: rerun the insights stage after a prompt change, export
again, and review the diff (`git diff tests/golden`). Run from the repo root after
`uw-pipeline run --ticker MAR --ticker JPM --ticker ENPH --stage insights`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

from uw_pipeline.clients.db import make_engine
from uw_pipeline.schema import company, followup_question, insight, insight_evidence
from uw_pipeline.settings import Settings

GOLDEN = ["MAR", "JPM", "ENPH"]
OUT = Path(__file__).resolve().parents[1] / "tests" / "golden"


def main() -> None:
    engine = make_engine(Settings().database_url)
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
                .where(insight.c.company_id == company_id, insight.c.is_current.is_(True))
                .order_by(insight.c.kind, insight.c.ordinal)
            ).mappings()
            insights = []
            for r in rows:
                evidence = conn.execute(
                    select(insight_evidence.c.source_kind, insight_evidence.c.section_code,
                           insight_evidence.c.metric_code, insight_evidence.c.snippet)
                    .where(insight_evidence.c.insight_id == r["insight_id"])
                ).mappings()  # fmt: skip
                insights.append(
                    {k: r[k] for k in ("kind", "category", "subcategory", "insight_type",
                                       "title", "summary", "uw_relevance", "prompt_version")}
                    | {"evidence": [dict(e) for e in evidence]}
                )  # fmt: skip
            questions = [
                {k: q[k] for k in ("category", "question", "rationale", "system_priority")}
                for q in conn.execute(
                    select(followup_question)
                    .where(followup_question.c.company_id == company_id,
                           followup_question.c.is_current.is_(True))
                    .order_by(followup_question.c.ordinal)
                ).mappings()
            ]  # fmt: skip
            path = OUT / f"{ticker}.json"
            path.write_text(
                json.dumps({"insights": insights, "followups": questions}, indent=1) + "\n"
            )
            print(f"wrote {path.relative_to(OUT.parents[1])}: {len(insights)} insights, "
                  f"{len(questions)} questions")  # fmt: skip


if __name__ == "__main__":
    main()
