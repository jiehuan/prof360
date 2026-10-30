"""Coverage report: per company and metric, which periods have values and which tags fed them.

Every core metric short of the target history (5 fiscal years, 8 quarters) must have an
explanation in config/coverage_notes.yaml, or it is flagged UNEXPLAINED. A company that began
filing 10-Ks and 10-Qs recently (a listing, a spin-off, a move from 20-F) is held to what its
filings can hold (`history_start`, Phase 17e).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, select

from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.schema import xbrl_fact
from uw_pipeline.yaml_load import safe_load

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
    # A formula over a text metric (a product's share of revenue) can only reach as far back
    # as the text it is built on, so it gets the text target (Phase 16).
    from_text: bool = False
    # The company's target where its own filings start inside the window (history_start).
    target_fy: int = TARGET_FY
    target_q: int = TARGET_Q

    def target(self, metric: MetricDef) -> tuple[int, int]:
        if metric.kind == "text" or self.from_text:
            return 0, TARGET_TEXT_Q
        fy = self.target_fy if "FY" in metric.period_types else 0
        q = self.target_q if "Q" in metric.period_types else 0
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


@dataclass
class CoverageNotes:
    """coverage_notes.yaml: notes per company (ticker -> metric -> note, "*" for all its
    metrics); from Phase 16 per industry group (`groups:` group -> metric -> note) for gaps
    every company of a group shares, group "*" for every company; from Phase 17e per company
    trait (`traits:`, e.g. clinical_stage). A company note wins over its traits', a trait's
    over its group's, a group's over "*"."""

    companies: dict[str, dict[str, str]] = field(default_factory=dict)
    groups: dict[str, dict[str, str]] = field(default_factory=dict)
    traits: dict[str, dict[str, str]] = field(default_factory=dict)

    def for_company(
        self, ticker: str, group: str | None, traits: Iterable[str] = ()
    ) -> dict[str, str]:
        out = self.groups.get("*", {}) | self.groups.get(group or "", {})
        for t in traits:
            out |= self.traits.get(t, {})
        return out | self.companies.get(ticker.upper(), {})


def load_notes(config_dir: Path) -> CoverageNotes:
    path = config_dir / "coverage_notes.yaml"
    if not path.exists():
        return CoverageNotes()
    with path.open(encoding="utf-8") as f:
        raw = safe_load(f) or {}
    groups = raw.pop("groups", None) or {}
    traits = raw.pop("traits", None) or {}
    return CoverageNotes({t.upper(): notes for t, notes in raw.items()},
                         {g.upper(): notes for g, notes in groups.items()}, traits)  # fmt: skip


@dataclass(frozen=True)
class HistoryStart:
    """What a company's first own 10-K / 10-Q can hold: fiscal years from its earliest balance
    sheet (the prior year-end shown in that filing), quarters from its first 10-Q."""

    fy_from: str  # period end, ISO date
    q_from: str | None


def history_start(conn: Connection, company_id: int) -> HistoryStart | None:
    """From the company's XBRL facts (its own and its predecessor registrants')."""
    forms = ("10-K", "10-Q")
    first = conn.execute(
        select(xbrl_fact.c.accession_no).where(xbrl_fact.c.company_id == company_id,
                                               xbrl_fact.c.form.in_(forms))
        .order_by(xbrl_fact.c.filed_at, xbrl_fact.c.accession_no).limit(1)
    ).scalar()  # fmt: skip
    if first is None:
        return None
    # Total assets: on every balance sheet, and only there (the equity statement's opening
    # balance reaches a year further back than the balance sheets shown).
    fy_from = conn.execute(
        select(func.min(xbrl_fact.c.end_date)).where(
            xbrl_fact.c.company_id == company_id, xbrl_fact.c.accession_no == first,
            xbrl_fact.c.tag == "Assets", xbrl_fact.c.dims == "")
    ).scalar()  # fmt: skip
    first_q = conn.execute(
        select(xbrl_fact.c.accession_no).where(xbrl_fact.c.company_id == company_id,
                                               xbrl_fact.c.form == "10-Q")
        .order_by(xbrl_fact.c.filed_at, xbrl_fact.c.accession_no).limit(1)
    ).scalar()  # fmt: skip
    q_from = conn.execute(
        select(func.max(xbrl_fact.c.end_date)).where(
            xbrl_fact.c.company_id == company_id, xbrl_fact.c.accession_no == first_q,
            xbrl_fact.c.start_date.is_not(None))
    ).scalar() if first_q else None  # fmt: skip
    return HistoryStart(fy_from, q_from) if fy_from else None


def _earliest(periods: Iterable[tuple[Any, str]], since: str) -> Any:
    """The first period ending on or after `since`, with a week's leeway for 52/53-week
    years whose period end is not the calendar date."""
    leeway = (date.fromisoformat(since) - timedelta(days=7)).isoformat()
    return min((p for p, end in periods if end >= leeway), default=None)


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
    since: HistoryStart | None = None,
) -> CompanyCoverage:
    """`rows` are this company's metric_value rows; `since`: see `history_start`."""
    anchor = [r for r in rows if r.get("derivation", "REPORTED") in WINDOW_DERIVATIONS]
    fys = [r["fiscal_year"] for r in anchor if r["period_type"] == "FY"]
    qs = [(r["fiscal_year"], r["fiscal_quarter"]) for r in anchor if r["period_type"] == "Q"]
    latest_fy = max(fys) if fys else None
    latest_q = max(qs) if qs else None
    fy_window = set(range(latest_fy - TARGET_FY + 1, latest_fy + 1)) if latest_fy else set()
    q_window = _quarter_window(latest_q, TARGET_Q) if latest_q else set()
    if since is not None:
        first_fy = _earliest(((r["fiscal_year"], r["period_end"]) for r in anchor
                              if r["period_type"] == "FY"), since.fy_from)  # fmt: skip
        fy_window = {y for y in fy_window if first_fy is None or y >= first_fy}
        first_q = _earliest((((r["fiscal_year"], r["fiscal_quarter"]), r["period_end"])
                             for r in anchor if r["period_type"] == "Q"),
                            since.q_from) if since.q_from else None  # fmt: skip
        q_window = {q for q in q_window if since.q_from and (first_q is None or q >= first_q)}

    cov = CompanyCoverage(ticker, latest_fy, latest_q)
    metrics = list(metrics)
    text_codes = {m.code for m in metrics if m.kind == "text"}
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
        # XBRL values list their tags; text values name their document (one string).
        tags = sorted(
            {t for r in recent for ts in json.loads(r["source_tags"]).values()
             for t in (["filing text"] if isinstance(ts, str) else ts)}
        )  # fmt: skip
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
                    from_text=bool(m.inputs() & text_codes),
                    target_fy=len(fy_window) if since else TARGET_FY,
                    target_q=len(q_window) if since else TARGET_Q,
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
