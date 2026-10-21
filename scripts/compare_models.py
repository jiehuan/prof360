"""Compare a candidate model with the current synthesis model on the company-level tasks.

For each company, the strategic & risk, key insight and follow-up prompts are rebuilt exactly
as the insights stage builds them (key insights and follow-ups get the current stored insights
as their input, so each task is compared on the same input). The candidate's output is grounded
with the same rules and NOT stored; only the LLM call is cached. Writes
data/reports/model-compare-<model>.md with drop rates, cost and the titles side by side.

    python scripts/compare_models.py --model claude-sonnet-5-5 --effort medium MAR JPM ENPH
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, true

from uw_pipeline.clients.db import pipeline_engine
from uw_pipeline.clients.llm import ClaudeClient
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.enrich.insights import TaskRunner, ground_items
from uw_pipeline.enrich.llm import PROMPTS_DIR, ModelSpec, load_llm_config
from uw_pipeline.enrich.package import build_package, load_insights_config
from uw_pipeline.enrich.schemas import FollowUpQuestions, KeyInsights, StrategicInsights
from uw_pipeline.jobs.run_insights import _insight_source, _stored_set
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.schema import followup_question, llm_call
from uw_pipeline.settings import Settings


def _cost(conn: Any, model: str, since: datetime, pricing: dict[str, Any]) -> float:
    rows = conn.execute(
        select(llm_call.c.input_tokens, llm_call.c.output_tokens)
        .where(llm_call.c.model == model, llm_call.c.created_at >= since)
    ).all()  # fmt: skip
    pin, pout = pricing[model]
    return float(sum((i or 0) * pin + (o or 0) * pout for i, o in rows)) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("tickers", nargs="+")
    args = ap.parse_args()

    s = Settings()
    assert s.anthropic_api_key is not None, "ANTHROPIC_API_KEY is not set"
    client = ClaudeClient(s.anthropic_api_key.get_secret_value(),
                          workspace_id=s.anthropic_workspace_id)  # fmt: skip
    icfg = load_insights_config(s.config_dir, s.llm_provider)
    llm_cfg = load_llm_config(s.config_dir, s.llm_provider)
    registry, industry = load_registry(s.config_dir), load_industry_config(s.config_dir)
    companies = {c.ticker: c for c in load_companies(s.config_dir)}
    cand = ModelSpec(model=args.model, effort=args.effort, max_tokens=icfg.synthesis.max_tokens)
    v = icfg.prompt_versions
    started = datetime.now(UTC)
    lines = [f"# {args.model} ({args.effort}) vs {icfg.synthesis.model} "
             f"({icfg.synthesis.effort})", ""]  # fmt: skip
    summary = ["| Company | Task | Current kept | Candidate kept/generated |", "|---|---|---|---|"]
    engine = pipeline_engine(s)
    with engine.begin() as conn:
        common = (PROMPTS_DIR / f"{v['common']}.md").read_text(encoding="utf-8")
        runner = TaskRunner(client, conn, lambda: datetime.now(UTC), common)
        for t in args.tickers:
            row = company_row(conn, companies[t])
            group = industry.group_of(row["industry_group"], companies[t])
            metrics = registry.for_company(t, group).metrics
            since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=icfg.news_days)
            pkg = build_package(conn, row, metrics, group.label, icfg, news_since=since)
            cur = {k: _stored_set(conn, row["company_id"], k)
                   for k in ("STRATEGIC_RISK", "BEHAVIORAL", "KEY")}  # fmt: skip
            lines += [f"## {t}", ""]

            # strategic & risk (same input as the stage)
            user_r = pkg.render("METRIC", "SIGNAL", "DOCUMENT", "TRANSCRIPT", "NEWS")
            out_r = runner.run(cand, "strategic_risk", v["strategic_risk"], user_r,
                               StrategicInsights)  # fmt: skip
            fields = ["title", "description", "uw_relevance"]
            kept_r, g_r = ground_items(out_r.insights if out_r else [], pkg, fields, "strategic")
            cur_r = [r for r in cur["STRATEGIC_RISK"] if r["subcategory"] != "No signal"]
            summary.append(
                f"| {t} | strategic & risk | {len(cur_r)} | {g_r.kept}/{g_r.generated} |"
            )
            lines += ["### Strategic & risk", "", "| Current | Candidate |", "|---|---|"]
            cand_r = [f"{i.category}: {i.title}" for i, _ in kept_r]
            cur_titles = [f"{r['category']}: {r['title']}" for r in cur_r]
            for a, b in zip(cur_titles + [""] * len(cand_r), cand_r + [""] * len(cur_titles),
                            strict=False):  # fmt: skip
                if a or b:
                    lines.append(f"| {a} | {b} |")
            lines.append("")

            # key insights and follow-ups on the current (stored) earlier insights
            for prefix, rows in (("B", cur["BEHAVIORAL"]), ("R", cur["STRATEGIC_RISK"])):
                for n, r in enumerate(rows, start=1):
                    pkg.add(_insight_source(prefix, n, r))
            user_k = pkg.render("METRIC", "SIGNAL", "INSIGHT", "NEWS")
            out_k = runner.run(cand, "key_insights", v["key_insights"], user_k, KeyInsights)
            kept_k, g_k = ground_items(out_k.insights if out_k else [], pkg,
                                       ["title", "summary", "uw_relevance", "supporting_evidence"],
                                       "key insight")  # fmt: skip
            summary.append(
                f"| {t} | key insights | {len(cur['KEY'])} | {g_k.kept}/{g_k.generated} |"
            )
            lines += ["### Key insights", "", "| Current | Candidate |", "|---|---|"]
            a_k = [f"{r['insight_type']}: {r['title']}" for r in cur["KEY"]]
            b_k = [f"{i.type}: {i.title}" for i, _ in kept_k]
            for a, b in zip(a_k + [""] * len(b_k), b_k + [""] * len(a_k), strict=False):
                if a or b:
                    lines.append(f"| {a} | {b} |")
            lines.append("")

            for n, r in enumerate(cur["KEY"], start=1):
                pkg.add(_insight_source("K", n, r))
            user_q = pkg.render("METRIC", "INSIGHT")
            out_q = runner.run(cand, "followups", v["followups"], user_q, FollowUpQuestions)
            cur_q = conn.execute(
                select(followup_question.c.question)
                .where(followup_question.c.company_id == row["company_id"],
                       followup_question.c.is_current == true())
                .order_by(followup_question.c.ordinal)
            ).scalars().all()  # fmt: skip
            n_q = len(out_q.questions) if out_q else 0
            summary.append(f"| {t} | follow-ups | {len(cur_q)} | {n_q} generated |")
            lines += ["### Follow-up questions", "", "| Current | Candidate |", "|---|---|"]
            b_q = [q.question for q in out_q.questions] if out_q else []
            for a, b in zip(list(cur_q) + [""] * len(b_q), b_q + [""] * len(cur_q), strict=False):
                if a or b:
                    lines.append(f"| {a} | {b} |")
            lines.append("")
        cost = _cost(conn, args.model, started, llm_cfg.pricing)
    report = [*lines[:2], f"Candidate cost for this comparison: ${cost:.2f}", "", *summary, "",
              *lines[2:]]  # fmt: skip
    out = s.data_dir / "reports" / f"model-compare-{args.model}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"wrote {out} (candidate cost ${cost:.2f})")


if __name__ == "__main__":
    main()
