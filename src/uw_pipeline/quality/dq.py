"""Data-quality rules (Phase 7, decisions D-035). Each rule reads the database and returns
findings for one company; the `dq` stage stores them per run."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, desc, func, select

from uw_pipeline.compute.coverage import company_coverage
from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.schema import (
    llm_task_stat,
    metric_value,
    release_check,
    signal,
    source_document,
)


class _Rule(BaseModel):
    severity: str


class QuarterJump(_Rule):
    metrics: list[str]
    max_change: float
    min_base_usd: float
    quarters: int


class NegativeValue(_Rule):
    metrics: list[str]
    quarters: int
    years: int


class QuartersVsYear(_Rule):
    metrics: list[str]
    tolerance: float
    years: int


class ReleaseVsXbrl(_Rule):
    days: int


class LlmDropRate(_Rule):
    max_rate: float
    min_generated: int
    days: int


class StaleFilings(_Rule):
    days: int


class ReviewQueue(_Rule):
    statuses: list[str]


class DqConfig(BaseModel):
    missing_metric: _Rule
    quarter_jump: QuarterJump
    negative_value: NegativeValue
    quarters_vs_year: QuartersVsYear
    release_vs_xbrl: ReleaseVsXbrl
    llm_drop_rate: LlmDropRate
    stale_filings: StaleFilings
    review_queue: ReviewQueue


def load_dq_config(config_dir: Path) -> DqConfig:
    with (config_dir / "dq_rules.yaml").open(encoding="utf-8") as f:
        return DqConfig.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    metric_code: str | None = None
    period: str | None = None


def _label(ptype: str, fy: int, fq: int) -> str:
    return f"FY{fy}" if ptype == "FY" else f"FY{fy} Q{fq}"


def _values(conn: Connection, company_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            select(metric_value).where(metric_value.c.company_id == company_id)
        ).mappings()
    ]


# -- rules -------------------------------------------------------------------------------------


def missing_metric(
    ticker: str, metrics: Sequence[MetricDef], values: list[dict[str, Any]],
    notes: dict[str, str], cfg: _Rule,
) -> list[Finding]:  # fmt: skip
    cov = company_coverage(ticker, metrics, values, notes)
    return [
        Finding(
            "missing_metric",
            cfg.severity,
            f"{m.code}: {c.fy_count} of 5 fiscal years and {c.q_count} of 8 quarters, "
            "with no explanation in coverage_notes.yaml",
            m.code,
        )
        for m, c in cov.problems()
    ]


def quarter_jump(
    values: list[dict[str, Any]], metrics: dict[str, MetricDef], cfg: QuarterJump
) -> list[Finding]:
    """A jump both quarter-on-quarter and against the same quarter a year earlier: seasonality
    and one-off swings move one of the two, a scale or sign error moves both."""
    out = []
    series: dict[str, list[dict[str, Any]]] = {}
    for v in values:
        m = metrics.get(v["metric_code"])
        if (
            v["period_type"] == "Q"
            and v["metric_code"] in cfg.metrics
            and m is not None
            and m.unit == "USD"
            and v["value"] is not None
        ):
            series.setdefault(v["metric_code"], []).append(v)
    for code, rows in series.items():
        by_q = {(r["fiscal_year"], r["fiscal_quarter"]): r["value"] for r in rows}
        recent = sorted(by_q)[-cfg.quarters :]
        for fy, fq in recent:
            cur = by_q[(fy, fq)]
            prev = by_q.get((fy - 1, 4) if fq == 1 else (fy, fq - 1))
            year_ago = by_q.get((fy - 1, fq))
            changes = []
            for base in (prev, year_ago):
                if base is None or abs(base) < cfg.min_base_usd:
                    break
                changes.append((cur - base) / abs(base))
            if len(changes) == 2 and all(abs(c) > cfg.max_change for c in changes):
                period = _label("Q", fy, fq)
                out.append(
                    Finding(
                        "quarter_jump", cfg.severity,
                        f"{code} {period} is {cur / 1e6:,.0f}M: {changes[0]:+.0%} vs the prior "
                        f"quarter and {changes[1]:+.0%} vs a year earlier", code, period,
                    )
                )  # fmt: skip
    return out


def negative_value(values: list[dict[str, Any]], cfg: NegativeValue) -> list[Finding]:
    latest_fy = max((v["fiscal_year"] for v in values), default=0)
    quarters = sorted(
        {(v["fiscal_year"], v["fiscal_quarter"]) for v in values if v["period_type"] == "Q"}
    )[-cfg.quarters :]
    out = []
    for v in values:
        if v["metric_code"] not in cfg.metrics or v["value"] is None or v["value"] >= 0:
            continue
        recent = (
            (v["fiscal_year"], v["fiscal_quarter"]) in quarters
            if v["period_type"] == "Q"
            else v["fiscal_year"] > latest_fy - cfg.years
        )
        if recent:
            period = _label(v["period_type"], v["fiscal_year"], v["fiscal_quarter"])
            out.append(
                Finding(
                    "negative_value",
                    cfg.severity,
                    f"{v['metric_code']} is negative ({v['value'] / 1e6:,.0f}M) in {period} "
                    f"[{v['derivation']}]",
                    v["metric_code"],
                    period,
                )
            )
    return out


def quarters_vs_year(values: list[dict[str, Any]], cfg: QuartersVsYear) -> list[Finding]:
    by_key = {
        (v["metric_code"], v["period_type"], v["fiscal_year"], v["fiscal_quarter"]): v["value"]
        for v in values
        if v["value"] is not None
    }
    years = sorted({v["fiscal_year"] for v in values if v["period_type"] == "FY"})[-cfg.years :]
    out = []
    for code in cfg.metrics:
        for fy in years:
            annual = by_key.get((code, "FY", fy, 0))
            quarters = [by_key.get((code, "Q", fy, q)) for q in (1, 2, 3, 4)]
            if annual is None or any(q is None for q in quarters):
                continue
            total = sum(q for q in quarters if q is not None)
            gap = abs(total - annual)
            if gap > cfg.tolerance * max(abs(annual), 1.0) and gap > 1e6:
                out.append(
                    Finding(
                        "quarters_vs_year",
                        cfg.severity,
                        f"{code} FY{fy}: quarters add up to {total / 1e6:,.0f}M but the year "
                        f"is {annual / 1e6:,.0f}M",
                        code,
                        f"FY{fy}",
                    )
                )
    return out


def release_vs_xbrl(
    conn: Connection, company_id: int, cfg: ReleaseVsXbrl, now: datetime
) -> list[Finding]:
    rows = conn.execute(
        select(release_check).where(
            release_check.c.company_id == company_id,
            release_check.c.agrees.is_(False),
            release_check.c.checked_at >= now - timedelta(days=cfg.days),
        )
    ).mappings()
    out = []
    for r in rows:
        period = _label(r["period_type"], r["fiscal_year"], r["fiscal_quarter"])
        out.append(
            Finding(
                "release_vs_xbrl",
                cfg.severity,
                f"{r['metric_code']} {period}: earnings release "
                f"{r['release_value'] / 1e6:,.0f}M vs XBRL {r['xbrl_value'] / 1e6:,.0f}M "
                f"(accession {r['accession_no']})",
                r["metric_code"],
                period,
            )
        )
    return out


def llm_drop_rate(
    conn: Connection, company_id: int, cfg: LlmDropRate, now: datetime
) -> list[Finding]:
    rows = conn.execute(
        select(llm_task_stat.c.task, func.sum(llm_task_stat.c.generated),
               func.sum(llm_task_stat.c.kept))
        .where(llm_task_stat.c.company_id == company_id,
               llm_task_stat.c.created_at >= now - timedelta(days=cfg.days))
        .group_by(llm_task_stat.c.task)
    ).all()  # fmt: skip
    out = []
    for task, generated, kept in rows:
        if not generated or generated < cfg.min_generated:
            continue
        rate = 1 - (kept or 0) / generated
        if rate > cfg.max_rate:
            out.append(
                Finding(
                    "llm_drop_rate",
                    cfg.severity,
                    f"{task}: grounding dropped {rate:.0%} of {generated} generated items "
                    f"in the last {cfg.days} days",
                )
            )
    return out


def stale_filings(
    conn: Connection, company_id: int, cfg: StaleFilings, now: datetime
) -> list[Finding]:
    latest = conn.execute(
        select(func.max(source_document.c.filed_at)).where(
            source_document.c.company_id == company_id,
            source_document.c.form_type.in_(("10-K", "10-Q")),
        )
    ).scalar_one_or_none()
    if latest is None:
        return [Finding("stale_filings", cfg.severity, "no 10-K or 10-Q on record")]
    age = (now.date() - datetime.fromisoformat(latest).date()).days
    if age > cfg.days:
        return [Finding("stale_filings", cfg.severity,
                        f"latest 10-K/10-Q was filed {latest} ({age} days ago)")]  # fmt: skip
    return []


def review_queue(conn: Connection, company_id: int, cfg: ReviewQueue) -> list[Finding]:
    rows = conn.execute(
        select(signal.c.title, signal.c.event_date, signal.c.review_status)
        .where(signal.c.company_id == company_id, signal.c.review_status.in_(cfg.statuses))
        .order_by(desc(signal.c.event_date))
    ).all()  # fmt: skip
    return [
        Finding("review_queue", cfg.severity, f"{status} signal ({date}): {title}")
        for title, date, status in rows
    ]


def run_rules(
    conn: Connection,
    company_id: int,
    ticker: str,
    metrics: Sequence[MetricDef],
    notes: dict[str, str],
    cfg: DqConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Finding]:
    stamp = now()
    values = _values(conn, company_id)
    by_code = {m.code: m for m in metrics}
    return [
        *missing_metric(ticker, metrics, values, notes, cfg.missing_metric),
        *quarter_jump(values, by_code, cfg.quarter_jump),
        *negative_value(values, cfg.negative_value),
        *quarters_vs_year(values, cfg.quarters_vs_year),
        *release_vs_xbrl(conn, company_id, cfg.release_vs_xbrl, stamp),
        *llm_drop_rate(conn, company_id, cfg.llm_drop_rate, stamp),
        *stale_filings(conn, company_id, cfg.stale_filings, stamp),
        *review_queue(conn, company_id, cfg.review_queue),
    ]
