"""Command-line entry point: `uw-pipeline ...`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

import typer
from sqlalchemy import select

from uw_pipeline.clients.db import ensure_schema, make_engine
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.sec import SecClient
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.compute.coverage import company_coverage, load_notes, render_markdown
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_companies, load_industry_config
from uw_pipeline.jobs.run_company import TaskResult, run_company_stage
from uw_pipeline.jobs.run_documents import run_documents_stage
from uw_pipeline.jobs.run_signals import run_signals_stage
from uw_pipeline.jobs.run_xbrl import company_row, run_compute_stage, run_facts_stage
from uw_pipeline.schema import metric_value
from uw_pipeline.settings import Settings
from uw_pipeline.signals.rules import load_signal_rules

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger("uw_pipeline")


class Stage(StrEnum):
    company = "company"
    facts = "facts"
    compute = "compute"
    documents = "documents"
    signals = "signals"


# Stages in execution order; later phases append here.
STAGE_ORDER = [Stage.company, Stage.facts, Stage.compute, Stage.documents, Stage.signals]


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
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def run(
    all_: Annotated[bool, typer.Option("--all", help="All active companies")] = False,
    ticker: Annotated[list[str] | None, typer.Option("--ticker", help="Repeatable")] = None,
    stage: Annotated[Stage | None, typer.Option(help="Run one stage (default: all)")] = None,
    force: Annotated[bool, typer.Option(help="Ignore same-day raw cache")] = False,
) -> None:
    """Run pipeline stages for the selected companies."""
    settings = Settings()
    selected = _select_companies(load_companies(settings.config_dir), all_, ticker or [])
    industry = load_industry_config(settings.config_dir)
    registry = load_registry(settings.config_dir)
    registry.check_groups(industry.groups.values())
    signal_rules = load_signal_rules(settings.config_dir)
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        ensure_schema(engine)

    storage = LocalStorage(settings.raw_dir)
    limiter = TokenBucket(rate=settings.sec_rate_limit_per_sec)
    results: list[TaskResult] = []
    with SecClient(
        settings.sec_user_agent,
        storage,
        rate_limiter=limiter,
        timeout=settings.http_timeout_sec,
        max_attempts=settings.sec_max_attempts,
    ) as sec:
        for s in [stage] if stage else STAGE_ORDER:
            if s is Stage.company:
                results += run_company_stage(selected, sec, industry, engine, force=force)
            elif s is Stage.facts:
                results += run_facts_stage(
                    selected, sec, registry, industry, engine,
                    history_years=settings.xbrl_history_years, force=force,
                )  # fmt: skip
            elif s is Stage.compute:
                results += run_compute_stage(selected, registry, industry, engine)
            elif s is Stage.documents:
                results += run_documents_stage(
                    selected, sec, registry, signal_rules, engine,
                    text_lookback_days=settings.text_lookback_days,
                    event_lookback_days=settings.event_lookback_days, force=force,
                )  # fmt: skip
            elif s is Stage.signals:
                results += run_signals_stage(selected, sec, signal_rules, engine)

    failed = [r for r in results if not r.ok]
    typer.echo(f"{len(results) - len(failed)}/{len(results)} tasks succeeded")
    for r in failed:
        typer.echo(f"  FAILED {r.ticker} [{r.stage}]: {r.detail}", err=True)
    if failed:
        raise typer.Exit(code=1)


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


@app.command("init-db")
def init_db() -> None:
    """Create tables in the local SQLite database."""
    settings = Settings()
    ensure_schema(make_engine(settings.database_url))
    typer.echo(f"schema ready at {settings.database_url}")


if __name__ == "__main__":
    app()
