"""Command-line entry point: `uw-pipeline ...`."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

import typer
from sqlalchemy import select

from uw_pipeline.clients.db import ensure_schema, make_engine
from uw_pipeline.compute.coverage import company_coverage, load_notes, render_markdown
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_companies, load_industry_config
from uw_pipeline.enrich.audit import audit
from uw_pipeline.jobs.orchestrator import Orchestrator, RunInProgressError, load_jobs_config
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.jobs.stages import StageRunner
from uw_pipeline.logs import log_context, setup_logging
from uw_pipeline.schema import company, dq_finding, metric_value, pipeline_run, pipeline_task
from uw_pipeline.schema_app import app_metadata
from uw_pipeline.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger("uw_pipeline")


class Stage(StrEnum):
    company = "company"
    facts = "facts"
    compute = "compute"
    documents = "documents"
    transcripts = "transcripts"
    news = "news"
    textmetrics = "textmetrics"
    signals = "signals"
    insights = "insights"
    metricdetail = "metricdetail"
    publish = "publish"
    scores = "scores"
    dq = "dq"


# Default order for manual runs (scheduled jobs list their stages in config/jobs.yaml).
STAGE_ORDER = [
    Stage.company, Stage.facts, Stage.compute, Stage.documents,
    Stage.transcripts, Stage.news, Stage.scores, Stage.textmetrics, Stage.signals,
    Stage.insights,
    Stage.metricdetail, Stage.publish, Stage.dq,
]  # fmt: skip


def _select_companies(
    companies: list[CompanyConfig], all_: bool, tickers: list[str]
) -> list[CompanyConfig]:
    if all_ == bool(tickers):
        raise typer.BadParameter("pass exactly one of --all or --ticker")
    if all_:
        return [c for c in companies if c.active]
    wanted = {t.upper() for t in tickers}
    by_ticker = {c.ticker: c for c in companies}
    for c in companies:
        for former in c.former_tickers:
            by_ticker.setdefault(former.upper(), c)
    unknown = wanted - by_ticker.keys()
    if unknown:
        raise typer.BadParameter(f"not in companies.yaml: {', '.join(sorted(unknown))}")
    return list({by_ticker[t].ticker: by_ticker[t] for t in sorted(wanted)}.values())


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    setup_logging(json_format=os.environ.get("LOG_FORMAT", "").lower() == "json", verbose=verbose)


@app.command()
def run(
    all_: Annotated[bool, typer.Option("--all", help="All active companies")] = False,
    ticker: Annotated[list[str] | None, typer.Option("--ticker", help="Repeatable")] = None,
    stage: Annotated[
        list[Stage] | None, typer.Option(help="Stage(s) to run, repeatable (default: all)")
    ] = None,
    job: Annotated[
        str | None, typer.Option(help="Scheduled job from config/jobs.yaml (e.g. nightly)")
    ] = None,
    force: Annotated[bool, typer.Option(help="Ignore same-day raw cache")] = False,
    resume: Annotated[
        bool, typer.Option(help="Resume this job's interrupted run (default for --job)")
    ] = True,
) -> None:
    """Run pipeline stages as a tracked run (pipeline_run / pipeline_task)."""
    settings = Settings()
    jobs_cfg = load_jobs_config(settings.config_dir)
    if job is not None:
        if job not in jobs_cfg.jobs:
            raise typer.BadParameter(f"unknown job {job!r}; see config/jobs.yaml")
        if stage:
            raise typer.BadParameter("--job and --stage are mutually exclusive")
        stages = jobs_cfg.jobs[job].stages
        all_ = all_ or not ticker
    else:
        stages = [s.value for s in (stage or STAGE_ORDER)]
    selected = _select_companies(load_companies(settings.config_dir), all_, ticker or [])
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        ensure_schema(engine)

    orch = Orchestrator(
        engine,
        stale_after=timedelta(minutes=jobs_cfg.stale_after_minutes),
        max_attempts=jobs_cfg.max_attempts,
    )
    try:
        run_id, resumed = orch.start(
            job or "manual", stages, [c.ticker for c in selected],
            trigger="cron" if job else "manual", resume=resume and job is not None,
        )  # fmt: skip
    except RunInProgressError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    started = datetime.now(UTC)
    with log_context(run_id=run_id, job=job or "manual"):
        log.info("run %d %s: %s for %s", run_id, "resumed" if resumed else "started",
                 stages, [c.ticker for c in selected])  # fmt: skip
        refresh = jobs_cfg.jobs[job].refresh_sec if job else False
        with StageRunner(settings, engine, force=force, refresh_sec=refresh) as runner:
            runner.run_id = run_id
            outcome = orch.execute(run_id, runner)
            orch.add_stats(run_id, {"usage_last_start": runner.usage(started)})
    counts = ", ".join(f"{k.lower()}={v}" for k, v in sorted(outcome.counts.items()))
    typer.echo(f"run {run_id} {outcome.status}: {counts}")
    with engine.connect() as conn:
        failed = conn.execute(
            select(pipeline_task.c.ticker, pipeline_task.c.stage, pipeline_task.c.status,
                   pipeline_task.c.detail)
            .where(pipeline_task.c.run_id == run_id,
                   pipeline_task.c.status.in_(("FAILED", "SKIPPED")))
            .order_by(pipeline_task.c.ordinal)
        ).all()  # fmt: skip
    for t, st, status, detail in failed:
        typer.echo(f"  {status} {t} [{st}]: {detail}", err=True)
    if outcome.status != "SUCCEEDED":
        raise typer.Exit(code=1)


@app.command()
def runs(limit: Annotated[int, typer.Option(help="Runs to show")] = 10) -> None:
    """Recent runs, and the tasks that did not succeed in the latest one."""
    settings = Settings()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        ensure_schema(engine)
    with engine.connect() as conn:
        rows = (
            conn.execute(select(pipeline_run).order_by(pipeline_run.c.run_id.desc()).limit(limit))
            .mappings()
            .all()
        )
        for r in rows:
            stats = json.loads(r["stats_json"] or "{}")
            cost = stats.get("usage_last_start", {}).get("llm_cost_usd_estimate", 0)
            ended = f"{r['finished_at']:%H:%M}" if r["finished_at"] else "running"
            typer.echo(
                f"{r['run_id']:>5} {r['job']:<8} {r['status']:<9} starts={r['starts']} "
                f"{r['started_at']:%Y-%m-%d %H:%M} -> {ended} "
                f"tasks={stats.get('tasks', {})} llm=${cost}"
            )
        if rows:
            bad = conn.execute(
                select(pipeline_task.c.ticker, pipeline_task.c.stage, pipeline_task.c.status,
                       pipeline_task.c.attempts, pipeline_task.c.detail)
                .where(pipeline_task.c.run_id == rows[0]["run_id"],
                       pipeline_task.c.status != "SUCCEEDED")
                .order_by(pipeline_task.c.ordinal)
            ).all()  # fmt: skip
            for t, st, status, attempts, detail in bad:
                typer.echo(f"  {status:<9} {t:<5} {st:<12} attempts={attempts} {detail or ''}")


@app.command("coverage-report")
def coverage_report(
    ticker: Annotated[list[str] | None, typer.Option("--ticker", help="Repeatable")] = None,
) -> None:
    """Write the XBRL metric coverage report (data/reports/coverage-<date>.md)."""
    settings = Settings()
    companies = load_companies(settings.config_dir)
    selected = _select_companies(companies, not ticker, ticker or [])
    industry = load_industry_config(settings.config_dir)
    registry = load_registry(settings.config_dir)
    notes = load_notes(settings.config_dir)
    engine = make_engine(settings.database_url)
    reports = []
    with engine.connect() as conn:
        for cfg in selected:
            row = company_row(conn, cfg)
            creg = registry.for_company(cfg.ticker, industry.groups[row["industry_group"]])
            values = [
                dict(r)
                for r in conn.execute(
                    select(metric_value).where(metric_value.c.company_id == row["company_id"])
                ).mappings()
            ]
            reports.append(
                company_coverage(cfg.ticker, creg.metrics, values, notes.get(cfg.ticker, {}))
            )
    today = datetime.now(UTC).date().isoformat()
    out = settings.data_dir / "reports" / f"coverage-{today}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(reports, today), encoding="utf-8")
    typer.echo(f"wrote {out}")
    problems = [(r.ticker, m.code, c.status(m)) for r in reports for m, c in r.problems()]
    for t, code, status in problems:
        typer.echo(f"  {status:12} {t} {code}")
    if problems:
        typer.echo(f"{len(problems)} metric gaps have no explanation in coverage_notes.yaml")


@app.command("audit-insights")
def audit_insights(sample: Annotated[int, typer.Option(help="Insights to sample")] = 20) -> None:
    """Re-check stored evidence against the sources and the detail-modal data (Phases 5, 8)."""
    settings = Settings()
    with make_engine(settings.database_url).connect() as conn:
        rep = audit(conn, sample=sample)
    pct = 100 * rep.snippets_found / rep.snippets if rep.snippets else 100.0
    typer.echo(f"sampled insights:            {rep.sampled_insights}")
    typer.echo(f"evidence snippets found:     {rep.snippets_found}/{rep.snippets} ({pct:.0f}%)")
    typer.echo(f"insights without evidence:   {rep.insights_without_evidence}")
    typer.echo(f"evidence without a source title: {rep.evidence_without_title}")
    typer.echo(f"questions without sources:   {rep.questions_without_sources}/{rep.questions}")
    typer.echo(f"loan rows found in filing:   {rep.loan_rows_found}/{rep.loan_rows}")
    typer.echo(f"metric details / relevance:  {rep.metric_details} / {rep.metric_relevance}")
    typer.echo(f"FI rows for non-FI companies: {rep.fi_rows_for_non_fi}")
    for f in rep.failures:
        typer.echo(f"  NOT FOUND {f}", err=True)
    if rep.failed:
        raise typer.Exit(code=1)


@app.command("dq-report")
def dq_report(
    run_id: Annotated[
        int | None, typer.Option(help="Run (default: latest with dq results)")
    ] = None,
) -> None:
    """Data-quality findings of a run (data/reports/dq-run<id>.md)."""
    settings = Settings()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        ensure_schema(engine)
    with engine.connect() as conn:
        if run_id is None:
            run_id = conn.execute(
                select(pipeline_task.c.run_id)
                .where(pipeline_task.c.stage == "dq", pipeline_task.c.status == "SUCCEEDED")
                .order_by(pipeline_task.c.run_id.desc())
                .limit(1)
            ).scalar()
        if run_id is None:
            typer.echo("no run with dq results yet; run `uw-pipeline run --job nightly`")
            raise typer.Exit(code=1)
        rows = conn.execute(
            select(company.c.ticker, dq_finding.c.severity, dq_finding.c.rule_id,
                   dq_finding.c.metric_code, dq_finding.c.period, dq_finding.c.message)
            .join(company, company.c.company_id == dq_finding.c.company_id)
            .where(dq_finding.c.run_id == run_id)
            .order_by(company.c.ticker, dq_finding.c.finding_id)
        ).all()  # fmt: skip
    order = {"Error": 0, "Warning": 1, "Info": 2}
    rows = sorted(rows, key=lambda r: (order.get(r.severity, 3), r.ticker))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    by_severity = sorted(counts.items(), key=lambda kv: order.get(kv[0], 3))
    summary = ", ".join(f"{k} {v}" for k, v in by_severity) or "no findings"
    lines = [f"# Data quality — run {run_id}", "", f"Severity: {summary}", ""]
    for sev in ("Error", "Warning", "Info"):
        mine = [r for r in rows if r.severity == sev]
        if not mine:
            continue
        lines += [f"## {sev} ({len(mine)})", "", "| Company | Rule | Message |", "|---|---|---|"]
        lines += [f"| {r.ticker} | {r.rule_id} | {r.message} |" for r in mine]
        lines.append("")
    out = settings.data_dir / "reports" / f"dq-run{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    typer.echo(f"wrote {out}")
    for r in rows:
        if r.severity != "Info":
            typer.echo(f"  {r.severity:<7} {r.ticker:<5} {r.rule_id:<17} {r.message}")
    typer.echo(f"{counts.get('Info', 0)} items in the review queue (Info), see the report")


@app.command("init-db")
def init_db() -> None:
    """Create tables in the local SQLite databases (pipeline and dev app database)."""
    settings = Settings()
    ensure_schema(make_engine(settings.database_url))
    typer.echo(f"schema ready at {settings.database_url}")
    app_engine = make_engine(settings.app_database_url)
    if app_engine.dialect.name == "sqlite":
        ensure_schema(app_engine, app_metadata)
        typer.echo(f"app schema ready at {settings.app_database_url}")


if __name__ == "__main__":
    app()
