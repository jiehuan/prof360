"""The input package for company-level LLM tasks (Phase 5b).

Every item gets an id (M = metric row, S = signal, E = filing excerpt, T = transcript,
N = news activity, K/R/B = earlier insights) so outputs can cite it and grounding can check the
quote against exactly that item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, desc, or_, select

from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.enrich.clinical import clinical_text, current_programs
from uw_pipeline.enrich.legal import legal_matters
from uw_pipeline.enrich.llm import ModelSpec, load_llm_config
from uw_pipeline.enrich.sources import SourceRef
from uw_pipeline.enrich.untrusted import data_block, one_line
from uw_pipeline.schema import (
    activity,
    board_composition,
    filing_section,
    metric_value,
    news_risk_score,
    review_decision,
    signal,
    source_document,
)
from uw_pipeline.yaml_load import safe_load


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


def load_insights_config(config_dir: Path, provider: str | None = None) -> InsightsConfig:
    """insights.yaml plus the active provider's synthesis model (llm.yaml)."""
    with (config_dir / "insights.yaml").open(encoding="utf-8") as f:
        raw = safe_load(f)
    synthesis = load_llm_config(config_dir, provider).synthesis
    return InsightsConfig.model_validate({**raw, "synthesis": synthesis.model_dump()})


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
                    parts.append(data_block("item", s.text, id=s.id, source=one_line(s.label)))
                else:
                    parts.append(f"{s.id} | {one_line(s.text)}")
        return "\n".join(parts)


# -- formatting --------------------------------------------------------------------------------


_VOLUME = {"count": "", "ounces": " oz", "pounds": " lb", "tons": " tons"}


def fmt_value(value: float | None, unit: str, status: str = "OK") -> str:
    if status == "NM" or value is None:
        return "N/M"
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    if unit == "ratio":
        return f"{value:.2f}x"
    if unit == "days":
        return f"{value:.0f} days"
    if unit == "months":  # cash runway (D-058)
        return f"{value:.1f} months"
    if unit == "shares":
        return f"{value / 1e6:,.1f}M shares"
    if unit == "score":
        return f"{value:.2f}"
    if unit in _VOLUME:  # members, vehicles, barrels per day (Phase 16); oz, lb, tons (Phase 17)
        suffix = _VOLUME[unit]
        if abs(value) >= 1e9:
            return f"{value / 1e9:,.2f}B{suffix}"
        return f"{value / 1e6:,.2f}M{suffix}" if abs(value) >= 1e6 else f"{value:,.0f}{suffix}"
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


_MATTER_ORDER = ("securities_class_action", "derivative", "regulatory_investigation",
                 "enforcement", "antitrust", "product_liability", "other")  # fmt: skip
MAX_MATTERS = 15


def legal_text(matters: list[dict[str, Any]]) -> str:
    """Litigation and regulatory matters (Phase 15e), most significant first, each with the
    filing's own words; the latest disclosed first within a type."""
    ranked = sorted(matters, key=lambda m: m["last_seen"], reverse=True)
    ranked.sort(key=lambda m: _MATTER_ORDER.index(m["matter_type"]))
    lines = []
    for m in ranked[:MAX_MATTERS]:
        parts = [m["matter_type"].replace("_", " "), m["status"]]
        if m["agency"]:
            parts.append(m["agency"])
        if m["started"]:
            parts.append(f"began {m['started']}")
        if m["accrual"]:
            parts.append(f"accrued {m['accrual'] / 1e6:,.1f}M USD")
        parts.append(f"last disclosed {m['last_seen']}")
        lines.append(f'{m["name"]} ({"; ".join(parts)}). "{m["quote"][:400]}"')
    return "\n".join(lines)


def board_text(board: Any) -> str:
    """Board facts (D-045) with the proxy's own words, so insights can quote them."""
    evidence = json.loads(board["evidence_json"])
    lines = []
    for key, label in (("board_size", "Directors"), ("independent_directors", "Independent"),
                       ("ceo_is_chair", "CEO is also chair"),
                       ("lead_independent_director", "Lead independent director")):  # fmt: skip
        value = board[key]
        if value is None:
            continue
        shown = {True: "yes", False: "no", "": "none"}.get(value, value) \
            if isinstance(value, bool | str) else value  # fmt: skip
        lines.append(f'{label}: {shown}. "{evidence.get(key, "")}"')
    for c in json.loads(board["committees_json"]):
        lines.append(f"{c['name']} chair: {c['chair']}")
    if board.get("ceo_name"):  # board_v2 facts (Phase 15f)
        since = f", CEO since {board['ceo_since']}" if board.get("ceo_since") else ""
        lines.append(f'CEO: {board["ceo_name"]}{since}. "{evidence.get("ceo_name", "")}"')
    if board.get("dual_class") is not None:
        shown = "yes" if board["dual_class"] else "no"
        lines.append(f'Dual-class common stock: {shown}. "{evidence.get("dual_class", "")}"')
    if board.get("audit_fees") is not None:
        parts = [("Audit", "audit_fees"), ("audit-related", "audit_related_fees"),
                 ("tax", "tax_fees"), ("other", "other_fees")]  # fmt: skip
        fees = ", ".join(f"{label} {board[k] / 1e6:,.1f}M USD" for label, k in parts
                         if board.get(k) is not None)  # fmt: skip
        lines.append(f"Auditor fees FY{board['fees_fiscal_year']}: {fees}.")
    return "\n".join(lines)


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

    defs = {m.code: m for m in metrics if m.display or m.insight_input}
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

    rejected = select(review_decision.c.item_key).where(
        review_decision.c.item_type == "SIGNAL", review_decision.c.decision == "REJECTED"
    )
    for i, s in enumerate(
        conn.execute(
            select(signal)
            .where(
                signal.c.company_id == company_id, signal.c.signal_key.not_in(rejected)
            )  # a reviewer rejected it (D-045)
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

    board = conn.execute(
        select(board_composition, source_document.c.filed_at)
        .join(source_document, source_document.c.doc_id == board_composition.c.doc_id)
        .where(board_composition.c.company_id == company_id)
    ).mappings().first()  # fmt: skip
    if board is not None:
        text = board_text(board)
        if text:
            e += 1
            pkg.add(Source(id=f"E{e}", kind="DOCUMENT",
                           label=f"DEF 14A filed {board['filed_at']}, board composition",
                           text=text, doc_id=board["doc_id"]))  # fmt: skip

    matters = legal_matters(conn, company_id)
    if matters:
        latest = max(matters, key=lambda m: m["last_seen"])
        e += 1
        pkg.add(Source(id=f"E{e}", kind="DOCUMENT",
                       label=f"Litigation and regulatory matters in 10-K/10-Q filings through "
                             f"{latest['last_seen']}",
                       text=legal_text(matters), doc_id=latest["last_doc_id"]))  # fmt: skip

    # Clinical-stage pipeline (Phase 17c, D-058): the current programs and their next milestones.
    programs = current_programs(conn, company_id)
    if programs:
        latest = max(programs, key=lambda p: p["filed_at"])
        e += 1
        pkg.add(Source(id=f"E{e}", kind="DOCUMENT",
                       label=f"Clinical pipeline in 10-K/10-Q filings through {latest['filed_at']}",
                       text=clinical_text(programs), doc_id=latest["doc_id"]))  # fmt: skip

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
