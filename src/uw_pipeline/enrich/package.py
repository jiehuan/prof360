"""The input package for company-level LLM tasks (Phase 5b).

Every item gets an id (M = metric row, S = signal, E = filing excerpt, T = transcript,
N = news activity, K/R/B = earlier insights) so outputs can cite it and grounding can check the
quote against exactly that item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, desc, or_, select

from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.enrich.llm import ModelSpec
from uw_pipeline.enrich.sources import SourceRef
from uw_pipeline.schema import (
    activity,
    filing_section,
    metric_value,
    news_risk_score,
    signal,
    source_document,
)


class Materiality(BaseModel):
    overall: float
    any_dimension: float


class InsightsConfig(BaseModel):
    synthesis: ModelSpec
    prompt_versions: dict[str, str]
    excerpts: dict[str, int]
    transcripts: int
    news_days: int
    news_materiality: Materiality
    activity_batch: int
    metric_quarters: int
    metric_years: int
    counts: dict[str, tuple[int, int]]


def load_insights_config(config_dir: Path) -> InsightsConfig:
    with (config_dir / "insights.yaml").open(encoding="utf-8") as f:
        return InsightsConfig.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # METRIC | SIGNAL | DOCUMENT | TRANSCRIPT | NEWS | INSIGHT
    label: str
    text: str
    doc_id: int | None = None
    section_code: str | None = None
    metric_code: str | None = None
    signal_id: int | None = None
    activity_id: int | None = None
    values: tuple[float, ...] = ()  # numeric values (metric rows) for number grounding
    accession_no: str | None = None  # metric rows: the filing the value comes from
    insight_id: int | None = None  # K/R/B items
    ref: SourceRef | None = None  # title and link when already known (stored evidence)


@dataclass
class Package:
    company: str
    industry: str
    is_financial: bool
    sources: dict[str, Source] = field(default_factory=dict)

    def add(self, src: Source) -> None:
        self.sources[src.id] = src

    def of(self, *kinds: str) -> list[Source]:
        return [s for s in self.sources.values() if s.kind in kinds]

    def render(self, *kinds: str) -> str:
        """Text for the prompt: a header plus the items of the given kinds."""
        kind_label = "financial institution" if self.is_financial else "non-financial"
        parts = [f"Company: {self.company}", f"Industry group: {self.industry} ({kind_label})"]
        for kind, title in (
            ("METRIC", "Metrics (id | metric | period | value | basis)"),
            ("SIGNAL", "Rule-based signals (id | date | severity | title | evidence)"),
            ("INSIGHT", "Insights from earlier steps"),
            ("NEWS", "News (id | date | headline | summary)"),
            ("DOCUMENT", "Filing excerpts"),
            ("TRANSCRIPT", "Earnings call transcripts"),
        ):
            if kind not in kinds:
                continue
            items = self.of(kind)
            if not items:
                parts.append(f"\n## {title}\n(none)")
                continue
            parts.append(f"\n## {title}")
            for s in items:
                if kind in ("DOCUMENT", "TRANSCRIPT"):
                    parts.append(f'<item id="{s.id}" source="{s.label}">\n{s.text}\n</item>')
                else:
                    parts.append(f"{s.id} | {s.text}")
        return "\n".join(parts)


# -- formatting --------------------------------------------------------------------------------


def fmt_value(value: float | None, unit: str, status: str = "OK") -> str:
    if status == "NM" or value is None:
        return "N/M"
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    if unit == "ratio":
        return f"{value:.2f}x"
    if abs(value) >= 1e9:
        return f"${value / 1e9:,.2f}B"
    if abs(value) >= 1e6:
        return f"${value / 1e6:,.0f}M"
    return f"${value:,.2f}"


def _period_label(ptype: str, fy: int, fq: int) -> str:
    return f"FY{fy}" if ptype == "FY" else f"FY{fy} Q{fq}"


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit] + "\n[... excerpt cut ...]"


# -- building ----------------------------------------------------------------------------------


def build_package(
    conn: Connection,
    row: dict[str, Any],
    metrics: list[MetricDef],
    group_label: str,
    cfg: InsightsConfig,
    *,
    news_since: Any,
) -> Package:
    company_id = row["company_id"]
    pkg = Package(row["legal_name"], group_label, bool(row["is_financial"]))

    defs = {m.code: m for m in metrics if m.display}
    rows = conn.execute(
        select(metric_value)
        .where(metric_value.c.company_id == company_id)
        .order_by(metric_value.c.metric_code, desc(metric_value.c.fiscal_year),
                  desc(metric_value.c.fiscal_quarter))
    ).mappings()  # fmt: skip
    taken: dict[tuple[str, str], int] = {}
    n = 0
    for r in rows:
        m = defs.get(r["metric_code"])
        if m is None:
            continue
        limit = cfg.metric_quarters if r["period_type"] == "Q" else cfg.metric_years
        slot = (r["metric_code"], r["period_type"])
        if taken.get(slot, 0) >= limit:
            continue
        taken[slot] = taken.get(slot, 0) + 1
        n += 1
        label = _period_label(r["period_type"], r["fiscal_year"], r["fiscal_quarter"])
        shown = fmt_value(r["value"], m.unit, r["value_status"])
        pkg.add(
            Source(
                id=f"M{n}", kind="METRIC",
                label=f"{m.display_name} {label}",
                text=f"{m.display_name} | {label} | {shown} | {r['derivation']}",
                metric_code=m.code,
                values=(r["value"],) if r["value"] is not None else (),
                accession_no=r["source_accn"] or None,
            )
        )  # fmt: skip

    for i, s in enumerate(
        conn.execute(
            select(signal)
            .where(signal.c.company_id == company_id)
            .order_by(desc(signal.c.event_date), signal.c.signal_id)
        ).mappings(),
        start=1,
    ):
        evidence = (s["snippet"] or "")[:400]
        status = (
            "" if s["review_status"] in ("RULE_ONLY", "UNANIMOUS") else f" [{s['review_status']}]"
        )
        pkg.add(
            Source(
                id=f"S{i}", kind="SIGNAL", label=s["title"],
                text=f"{s['event_date']} | {s['severity']} | {s['title']}{status} | {evidence}",
                signal_id=s["signal_id"], doc_id=s["doc_id"], section_code=s["section_code"],
            )
        )  # fmt: skip

    e = 0
    for code, limit in cfg.excerpts.items():
        hit = conn.execute(
            select(filing_section.c.text, source_document.c.doc_id, source_document.c.form_type,
                   source_document.c.filed_at)
            .join(source_document, source_document.c.doc_id == filing_section.c.doc_id)
            .where(source_document.c.company_id == company_id,
                   filing_section.c.section_code == code)
            .order_by(desc(source_document.c.filed_at))
            .limit(1)
        ).first()  # fmt: skip
        if hit is None:
            continue
        e += 1
        pkg.add(
            Source(
                id=f"E{e}", kind="DOCUMENT",
                label=f"{hit.form_type} filed {hit.filed_at}, section {code}",
                text=_cut(hit.text, limit), doc_id=hit.doc_id, section_code=code,
            )
        )  # fmt: skip

    transcripts = conn.execute(
        select(filing_section.c.text, source_document.c.doc_id, source_document.c.accession_no,
               source_document.c.period_end)
        .join(source_document, source_document.c.doc_id == filing_section.c.doc_id)
        .where(source_document.c.company_id == company_id,
               source_document.c.source_type == "TRANSCRIPT")
        .order_by(desc(source_document.c.period_end))
        .limit(cfg.transcripts)
    ).all()  # fmt: skip
    for i, t in enumerate(reversed(transcripts), start=1):  # oldest first
        quarter = t.accession_no.rsplit("-", 1)[-1]
        pkg.add(
            Source(
                id=f"T{i}", kind="TRANSCRIPT",
                label=f"earnings call {quarter} (period ended {t.period_end})",
                text=t.text, doc_id=t.doc_id, section_code="TRANSCRIPT",
            )
        )  # fmt: skip

    nr = news_risk_score.c
    mat = cfg.news_materiality
    material = or_(
        nr.overall >= mat.overall,
        *(col >= mat.any_dimension for col in (nr.litigation, nr.regulatory,
                                                nr.financial_distress, nr.governance,
                                                nr.operational)),
    )  # fmt: skip
    news = conn.execute(
        select(activity)
        .join(news_risk_score, nr.activity_id == activity.c.activity_id)
        .where(activity.c.company_id == company_id, activity.c.published_at >= news_since,
               material)
        .order_by(desc(activity.c.published_at))
    ).mappings()  # fmt: skip
    for i, a in enumerate(news, start=1):
        summary = a["summary"] or a["av_summary"] or ""
        pkg.add(
            Source(
                id=f"N{i}", kind="NEWS", label=a["title"],
                text=f"{a['published_at']:%Y-%m-%d} | {a['title']} | {summary}",
                activity_id=a["activity_id"],
            )
        )  # fmt: skip
    return pkg
