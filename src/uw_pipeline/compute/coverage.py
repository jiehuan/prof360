"""Coverage report: per company and metric, which periods have values and which tags fed them.

Every core metric short of the target history (5 fiscal years, 8 quarters) must have an
explanation in config/coverage_notes.yaml, or it is flagged UNEXPLAINED.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from uw_pipeline.compute.registry import MetricDef

TARGET_FY = 5
TARGET_Q = 8
# Text-extracted metrics only cover the last ~400 days of filings (D-032), and several are
# quarterly-only (e.g. LCR is a quarterly average).
TARGET_TEXT_Q = 4
# Periods are counted back from the latest period with XBRL data, so a PRELIMINARY quarter from
# an earnings release doesn't shift the window ahead of everything else.
WINDOW_DERIVATIONS = ("REPORTED", "DERIVED", "COMPUTED")


@dataclass
class MetricCoverage:
    code: str
    fy_count: int
    q_count: int
    latest: str
    nm_count: int
    tags: list[str]
    note: str | None = None

    def target(self, metric: MetricDef) -> tuple[int, int]:
        if metric.kind == "text":
            return 0, TARGET_TEXT_Q
        fy = TARGET_FY if "FY" in metric.period_types else 0
        q = TARGET_Q if "Q" in metric.period_types else 0
        return fy, q

    def status(self, metric: MetricDef) -> str:
        fy, q = self.target(metric)
        if self.fy_count >= fy and self.q_count >= q:
            return "OK"
        if self.note:
            return "EXPLAINED"
        return "MISSING" if self.fy_count + self.q_count == 0 else "UNEXPLAINED"


@dataclass
class CompanyCoverage:
    ticker: str
    latest_fy: int | None
    latest_q: tuple[int, int] | None
    metrics: list[tuple[MetricDef, MetricCoverage]] = field(default_factory=list)

    def problems(self) -> list[tuple[MetricDef, MetricCoverage]]:
        return [(m, c) for m, c in self.metrics if c.status(m) in ("MISSING", "UNEXPLAINED")]


def load_notes(config_dir: Path) -> dict[str, dict[str, str]]:
    path = config_dir / "coverage_notes.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {t.upper(): notes for t, notes in raw.items()}


def _quarter_window(latest: tuple[int, int], n: int) -> set[tuple[int, int]]:
    fy, fq = latest
    out = set()
    for _ in range(n):
        out.add((fy, fq))
        fy, fq = (fy - 1, 4) if fq == 1 else (fy, fq - 1)
    return out


def company_coverage(
    ticker: str,
    metrics: Iterable[MetricDef],
    rows: list[dict[str, Any]],
    notes: dict[str, str],
) -> CompanyCoverage:
    """`rows` are this company's metric_value rows."""
    anchor = [r for r in rows if r.get("derivation", "REPORTED") in WINDOW_DERIVATIONS]
    fys = [r["fiscal_year"] for r in anchor if r["period_type"] == "FY"]
    qs = [(r["fiscal_year"], r["fiscal_quarter"]) for r in anchor if r["period_type"] == "Q"]
    latest_fy = max(fys) if fys else None
    latest_q = max(qs) if qs else None
    fy_window = set(range(latest_fy - TARGET_FY + 1, latest_fy + 1)) if latest_fy else set()
    q_window = _quarter_window(latest_q, TARGET_Q) if latest_q else set()

    cov = CompanyCoverage(ticker, latest_fy, latest_q)
    for m in metrics:
        if not m.display:
            continue
        mine = [r for r in rows if r["metric_code"] == m.code]
        in_fy = [r for r in mine if r["period_type"] == "FY" and r["fiscal_year"] in fy_window]
        in_q = [
            r for r in mine
            if r["period_type"] == "Q" and (r["fiscal_year"], r["fiscal_quarter"]) in q_window
        ]  # fmt: skip
        recent = in_fy + in_q
        tags = sorted(
            {t for r in recent for ts in json.loads(r["source_tags"]).values() for t in ts}
        )
        latest = max((r["period_end"] for r in mine), default="")
        cov.metrics.append(
            (
                m,
                MetricCoverage(
                    code=m.code,
                    fy_count=len(in_fy),
                    q_count=len(in_q),
                    latest=latest,
                    nm_count=sum(1 for r in recent if r["value_status"] == "NM"),
                    tags=tags,
                    note=notes.get(m.code) or notes.get("*"),
                ),
            )
        )
    return cov


def render_markdown(reports: list[CompanyCoverage], generated: str) -> str:
    lines = [
        f"# XBRL metric coverage — {generated}",
        "",
        f"Target: {TARGET_FY} fiscal years and {TARGET_Q} quarters (counted back from each "
        "company's latest period). Explanations come from `config/coverage_notes.yaml`.",
        "",
    ]
    for rep in reports:
        lq = f"FY{rep.latest_q[0]} Q{rep.latest_q[1]}" if rep.latest_q else "-"
        lines += [
            f"## {rep.ticker}",
            "",
            f"Latest fiscal year: {rep.latest_fy or '-'} · latest quarter: {lq}",
            "",
            "| Metric | FY | Q | N/M | Latest | Status | Tags used | Note |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for m, c in rep.metrics:
            fy_t, q_t = c.target(m)
            tags = "<br>".join(c.tags) or "—"
            lines.append(
                f"| {m.code} | {c.fy_count}/{fy_t} | {c.q_count}/{q_t} | {c.nm_count or ''} "
                f"| {c.latest or '—'} | {c.status(m)} | {tags} | {c.note or ''} |"
            )
        lines.append("")
    return "\n".join(lines)
