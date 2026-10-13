"""Phase 8 `metricdetail` stage (decisions D-036): data for the metric detail modal.

Per company, for every displayed metric with data:
  1. methodology (rules, enrich/metric_detail.py)            -> metric_detail
  2. the filings behind the latest quarter and fiscal year   -> metric_source
  3. Underwriting Relevance plus the related insights and follow-up questions, in one Claude
     Opus 5.5 call per company                               -> metric_insight, insight_metric,
                                                                followup_question_metric
Step 3 runs only when its input (metric values, current insights and questions) changed; the
links it adds are marked link_source = METRIC_DETAIL and replaced on every new generation.
Without an LLM the rule-based steps still run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, insert, select, update

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.compute.registry import MetricDef, Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.grounding import ungrounded_numbers
from uw_pipeline.enrich.insights import TaskRunner
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig
from uw_pipeline.enrich.metric_detail import (
    LLM_DERIVATIONS,
    latest,
    methodology,
    period_label,
    shown_rows,
)
from uw_pipeline.enrich.package import InsightsConfig, fmt_value
from uw_pipeline.enrich.schemas import MetricRelevances
from uw_pipeline.enrich.sources import SourceLabels, SourceRef, SourceResolver
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import (
    followup_question,
    followup_question_metric,
    insight,
    insight_metric,
    llm_task_stat,
    metric_detail,
    metric_insight,
    metric_source,
    metric_value,
    source_document,
)

log = logging.getLogger(__name__)

TASK = "metric_detail"
LINK = "METRIC_DETAIL"


@dataclass
class Stats:
    metrics: int = 0
    sources: int = 0
    relevance: str = "skipped"
    dropped: list[str] = field(default_factory=list)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _values(conn: Connection, company_id: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        select(metric_value).where(metric_value.c.company_id == company_id)
    ).mappings():
        out.setdefault(r["metric_code"], []).append(dict(r))
    return out


# -- data sources ------------------------------------------------------------------------------


def value_source(
    conn: Connection, resolver: SourceResolver, display: str, row: dict[str, Any]
) -> SourceRef:
    """The document a stored value comes from. Text-extracted values point to the exact
    document (MD&A section or earnings release); XBRL values to the filing."""
    label = f"{display} {period_label(row)}"
    if row["derivation"] in LLM_DERIVATIONS:
        role = row["formula"].removeprefix("llm:") or "PRIMARY"
        doc_id = conn.execute(
            select(source_document.c.doc_id).where(
                source_document.c.accession_no == row["source_accn"],
                source_document.c.doc_role == role,
            )
        ).scalar()
        if doc_id is not None:
            m = re.search(r"section (\S+)", json.loads(row["source_tags"]).get("document", ""))
            ref = resolver.document(doc_id, m.group(1) if m else None)
            if ref is not None:
                return SourceRef(f"{label} — {ref.title}", ref.url, ref.date, doc_id, ref.form)
    return resolver.metric(label, row["source_accn"])


# -- LLM relevance -----------------------------------------------------------------------------


def _render(
    company: str,
    group_label: str,
    is_fi: bool,
    metrics: Sequence[MetricDef],
    values: dict[str, list[dict[str, Any]]],
    items: Sequence[tuple[str, str]],
) -> str:
    kind = "financial institution" if is_fi else "non-financial"
    parts = [f"Company: {company}", f"Industry group: {group_label} ({kind})",
             "\n## Metrics (code | name | recent values, newest first)"]  # fmt: skip
    for m in metrics:
        shown = shown_rows(values[m.code])
        vals = "; ".join(
            f"{period_label(r)} {fmt_value(r['value'], m.unit, r['value_status'])}" for r in shown
        )
        parts.append(f"{m.code} | {m.display_name} | {vals}")
    for title, prefix in (("Key insights", "K"), ("Strategic & risk insights", "R"),
                          ("Behavioral insights", "B"), ("Follow-up questions", "Q")):  # fmt: skip
        parts.append(f"\n## {title}")
        parts += [f"{i} | {text}" for i, text in items if i.startswith(prefix)] or ["(none)"]
    return "\n".join(parts)


def _current_items(
    conn: Connection, company_id: int
) -> tuple[list[tuple[str, str]], dict[str, int], dict[str, int]]:
    """(id, text) of current insights and questions, plus id -> insight_id / question_id."""
    items: list[tuple[str, str]] = []
    insight_ids: dict[str, int] = {}
    for kind, prefix in (("KEY", "K"), ("STRATEGIC_RISK", "R"), ("BEHAVIORAL", "B")):
        # Numbered like the insights stage numbers them (ordinal order, "No signal" rows
        # included), so K/R/B ids mean the same in both prompts.
        rows = conn.execute(
            select(insight)
            .where(insight.c.company_id == company_id, insight.c.kind == kind,
                   insight.c.is_current.is_(True))
            .order_by(insight.c.ordinal)
        ).mappings().all()  # fmt: skip
        for n, r in enumerate(rows, start=1):
            if r["subcategory"] == "No signal":
                continue
            head = r["insight_type"] or " / ".join(
                x for x in (r["category"], r["subcategory"]) if x
            )
            items.append((f"{prefix}{n}", f"{head} | {r['title']} | {r['summary']}"))
            insight_ids[f"{prefix}{n}"] = r["insight_id"]
    question_ids: dict[str, int] = {}
    qs = conn.execute(
        select(followup_question)
        .where(followup_question.c.company_id == company_id,
               followup_question.c.is_current.is_(True))
        .order_by(followup_question.c.ordinal)
    ).mappings().all()  # fmt: skip
    for n, q in enumerate(qs, start=1):
        items.append((f"Q{n}", f"{q['category']} | {q['question']}"))
        question_ids[f"Q{n}"] = q["question_id"]
    return items, insight_ids, question_ids


def _replace_links(
    conn: Connection,
    company_id: int,
    pairs_i: set[tuple[int, str]],
    pairs_q: set[tuple[int, str]],
) -> None:
    """Swap this stage's links for new ones; links chosen with the insight/question stay."""
    ins = select(insight.c.insight_id).where(insight.c.company_id == company_id)
    qs = select(followup_question.c.question_id).where(followup_question.c.company_id == company_id)
    conn.execute(delete(insight_metric).where(insight_metric.c.link_source == LINK,
                                              insight_metric.c.insight_id.in_(ins)))  # fmt: skip
    conn.execute(delete(followup_question_metric).where(
        followup_question_metric.c.link_source == LINK,
        followup_question_metric.c.question_id.in_(qs)))  # fmt: skip
    have_i = {(r[0], r[1]) for r in conn.execute(
        select(insight_metric.c.insight_id, insight_metric.c.metric_code)
        .where(insight_metric.c.insight_id.in_(ins)))}  # fmt: skip
    have_q = {(r[0], r[1]) for r in conn.execute(
        select(followup_question_metric.c.question_id, followup_question_metric.c.metric_code)
        .where(followup_question_metric.c.question_id.in_(qs)))}  # fmt: skip
    new_i = [{"insight_id": i, "metric_code": c, "link_source": LINK}
             for i, c in sorted(pairs_i - have_i)]  # fmt: skip
    new_q = [{"question_id": q, "metric_code": c, "link_source": LINK}
             for q, c in sorted(pairs_q - have_q)]  # fmt: skip
    if new_i:
        conn.execute(insert(insight_metric), new_i)
    if new_q:
        conn.execute(insert(followup_question_metric), new_q)


def relevance(
    conn: Connection,
    runner: TaskRunner,
    icfg: InsightsConfig,
    row: dict[str, Any],
    group_label: str,
    metrics: Sequence[MetricDef],
    values: dict[str, list[dict[str, Any]]],
    now: datetime,
    stats: Stats,
) -> None:
    company_id = row["company_id"]
    items, insight_ids, question_ids = _current_items(conn, company_id)
    user = _render(row["legal_name"], group_label, bool(row["is_financial"]), metrics, values,
                   items)  # fmt: skip
    version = icfg.prompt_versions[TASK]
    spec = icfg.synthesis
    # Database ids are part of the hash: insights re-stored with the same text get new ids, and
    # the links must follow them (the LLM output itself then comes from the cache).
    ids = json.dumps([sorted(insight_ids.items()), sorted(question_ids.items())])
    input_hash = _hash(version, icfg.prompt_versions["common"], spec.model, user, ids)
    current = conn.execute(
        select(metric_insight.c.input_hash).where(metric_insight.c.company_id == company_id,
                                                  metric_insight.c.is_current.is_(True)).limit(1)
    ).scalar()  # fmt: skip
    if current == input_hash:
        stats.relevance = "unchanged"
        return
    out = runner.run(spec, TASK, version, user, MetricRelevances)
    if out is None:
        stats.relevance = "no output"
        return
    known = {m.code for m in metrics}
    numbers = [r["value"] for rows in values.values() for r in rows if r["value"] is not None]
    texts = [t for _, t in items]
    kept: dict[str, Any] = {}
    for item in out.metrics:
        code = item.metric_code.strip()
        if code not in known or code in kept:
            continue
        missing = ungrounded_numbers(item.uw_relevance, texts, numbers)
        if missing:
            stats.dropped.append(f"{code}: unverified numbers {missing}")
            continue
        kept[code] = item
    stats.relevance = f"{len(kept)}/{len(known)}"
    conn.execute(insert(llm_task_stat).values(company_id=company_id, task=TASK,
                                              generated=len(out.metrics), kept=len(kept),
                                              created_at=now))  # fmt: skip
    conn.execute(update(metric_insight).where(metric_insight.c.company_id == company_id)
                 .values(is_current=False))  # fmt: skip
    if kept:
        conn.execute(insert(metric_insight), [
            {"company_id": company_id, "metric_code": code, "uw_relevance": item.uw_relevance,
             "is_current": True, "model": spec.model, "prompt_version": version,
             "input_hash": input_hash, "created_at": now}
            for code, item in kept.items()
        ])  # fmt: skip
    pairs_i = {(insight_ids[i.strip()], code) for code, item in kept.items()
               for i in item.related_insight_ids if i.strip() in insight_ids}  # fmt: skip
    pairs_q = {(question_ids[q.strip()], code) for code, item in kept.items()
               for q in item.related_question_ids if q.strip() in question_ids}  # fmt: skip
    _replace_links(conn, company_id, pairs_i, pairs_q)


# -- company -----------------------------------------------------------------------------------


def process_company(
    cfg: CompanyConfig,
    registry: Registry,
    industry: IndustryConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    icfg: InsightsConfig,
    labels: SourceLabels,
    conn: Connection,
    now: datetime,
) -> Stats:
    row = company_row(conn, cfg)
    company_id = row["company_id"]
    group = industry.groups[row["industry_group"]]
    all_metrics = registry.for_company(cfg.ticker, group).metrics
    values = _values(conn, company_id)
    shown = [m for m in all_metrics if m.display and values.get(m.code)]
    names = {m.code: m.display_name for m in all_metrics} | labels.concepts
    resolver = SourceResolver(conn, row, labels)
    stats = Stats(metrics=len(shown))

    details, sources = [], []
    for m in shown:
        rows = values[m.code]
        top = latest(rows, "Q") or latest(rows, "FY")
        details.append({
            "company_id": company_id, "metric_code": m.code,
            "methodology": methodology(m, rows, names, labels),
            "latest_period": period_label(top) if top else None,
            "latest_value_text":
                fmt_value(top["value"], m.unit, top["value_status"]) if top else None,
            "updated_at": now,
        })  # fmt: skip
        for ptype in ("Q", "FY"):
            r = latest(rows, ptype)
            if r is None:
                continue
            ref = value_source(conn, resolver, m.display_name, dict(r))
            sources.append({
                "company_id": company_id, "metric_code": m.code, "period_type": ptype,
                "fiscal_year": r["fiscal_year"], "fiscal_quarter": r["fiscal_quarter"],
                "derivation": r["derivation"], "title": ref.title[:500], "url": ref.url,
                "form_type": ref.form, "filed_at": ref.date, "accession_no": r["source_accn"],
                "updated_at": now,
            })  # fmt: skip
    upsert(conn, metric_detail, details, key_cols=["company_id", "metric_code"])
    upsert(conn, metric_source, sources, key_cols=["company_id", "metric_code", "period_type"])
    codes = [m.code for m in shown]
    conn.execute(
        delete(metric_detail).where(and_(metric_detail.c.company_id == company_id,
                                         metric_detail.c.metric_code.not_in(codes)))
    )  # fmt: skip
    kept_src = {(s["metric_code"], s["period_type"]) for s in sources}
    for code, ptype in conn.execute(
        select(metric_source.c.metric_code, metric_source.c.period_type)
        .where(metric_source.c.company_id == company_id)
    ).all():  # fmt: skip
        if (code, ptype) not in kept_src:
            conn.execute(delete(metric_source).where(
                metric_source.c.company_id == company_id, metric_source.c.metric_code == code,
                metric_source.c.period_type == ptype))  # fmt: skip
    stats.sources = len(sources)

    if llm is not None and shown:
        client, _ = llm
        common = (PROMPTS_DIR / f"{icfg.prompt_versions['common']}.md").read_text(encoding="utf-8")
        runner = TaskRunner(client, conn, lambda: now, common)
        relevance(conn, runner, icfg, row, group.label, shown, values, now, stats)
    return stats


def run_metric_detail_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    icfg: InsightsConfig,
    labels: SourceLabels,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                stats = process_company(cfg, registry, industry, llm, icfg, labels, conn, now())
            for d in stats.dropped:
                log.info("%s: metric relevance dropped %s", cfg.ticker, d)
            detail = (f"{stats.metrics} metrics, {stats.sources} sources, "
                      f"relevance={stats.relevance}")  # fmt: skip
            log.info("%s: metricdetail %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "metricdetail", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: metricdetail stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "metricdetail", ok=False, detail=str(exc)))
    return results
