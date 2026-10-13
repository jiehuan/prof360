"""Phase 5a `textmetrics` stage (decisions D-032).

- Text metrics (`kind: text` in metrics.yaml: RevPAR, CET1, ...) from MD&A chunks and earnings
  releases -> metric_value, derivation LLM_EXTRACTED.
- Preliminary results from earnings releases (decisions D-022) -> metric_value, derivation
  PRELIMINARY, only for periods without an XBRL value. Where XBRL exists, the release value is
  cross-checked against it instead.
- Bank loan book by segment -> loan_portfolio_segment.
Bank metrics (`dual_check`) and the loan book are kept only where Claude Sonnet 5.5 and Claude
Opus 5.5 extract the same number independently.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine, and_, delete, insert, select, tuple_

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.storage import Storage
from uw_pipeline.compute.registry import MetricDef, Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, ModelSpec, cached_call
from uw_pipeline.enrich.schemas import LoanBook, TextValues, strict_json_schema
from uw_pipeline.enrich.text_metrics import (
    Candidate,
    TextConfig,
    agree,
    candidates,
    dual_checked,
    loan_rows,
    select_chunks,
)
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import RULE_DERIVATIONS, company_row, read_facts
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.periods import FiscalCalendar, learn_calendar
from uw_pipeline.normalize.sections import chunk
from uw_pipeline.schema import (
    filing_section,
    llm_task_stat,
    loan_portfolio_segment,
    metric_value,
    release_check,
    section_chunk,
    source_document,
)

log = logging.getLogger(__name__)

LLM_DERIVATIONS = ("LLM_EXTRACTED", "PRELIMINARY")
PRELIMINARY_KEYWORDS = ["revenue", "net income", "operating income", "net revenue"]
VALUES_SCHEMA = strict_json_schema(TextValues)
LOAN_SCHEMA = strict_json_schema(LoanBook)


@dataclass
class Stats:
    documents: int = 0
    extracted: int = 0
    rejected: int = 0
    dual_dropped: int = 0
    conflicts: int = 0
    preliminary: int = 0
    crosscheck_ok: int = 0
    crosscheck_mismatch: int = 0
    loan_segments: int = 0
    mismatches: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocText:
    doc_id: int
    accession_no: str
    form_type: str
    doc_role: str
    filed_at: str
    label: str  # e.g. "10-Q filed 2026-08-03, MD&A"
    chunks: list[str]


def _mdna_docs(conn: Connection, company_id: int, codes: Sequence[str]) -> list[DocText]:
    rows = conn.execute(
        select(
            source_document.c.doc_id, source_document.c.accession_no, source_document.c.form_type,
            source_document.c.filed_at, filing_section.c.section_id,
            filing_section.c.section_code,
        )
        .join(filing_section, filing_section.c.doc_id == source_document.c.doc_id)
        .where(
            source_document.c.company_id == company_id,
            source_document.c.doc_role == "PRIMARY",
            filing_section.c.section_code.in_(codes),
        )
    ).all()  # fmt: skip
    docs = []
    for r in rows:
        texts = conn.execute(
            select(section_chunk.c.text)
            .where(section_chunk.c.section_id == r.section_id)
            .order_by(section_chunk.c.chunk_index)
        ).scalars()
        docs.append(
            DocText(r.doc_id, r.accession_no, r.form_type, "PRIMARY", r.filed_at,
                    f"{r.form_type} filed {r.filed_at}, section {r.section_code}", list(texts))
        )  # fmt: skip
    return docs


def _release_docs(conn: Connection, company_id: int, storage: Storage) -> list[DocText]:
    rows = conn.execute(
        select(source_document).where(
            source_document.c.company_id == company_id,
            source_document.c.doc_role == "EX-99.1",
            source_document.c.raw_path.is_not(None),
        )
    ).mappings()
    docs = []
    for r in rows:
        text = html_to_text(storage.read_bytes(r["raw_path"]))
        docs.append(
            DocText(r["doc_id"], r["accession_no"], r["form_type"], "EX-99.1", r["filed_at"],
                    f"earnings release (8-K EX-99.1) filed {r['filed_at']}",
                    [c.text for c in chunk(text)])
        )  # fmt: skip
    return docs


class Extractor:
    def __init__(
        self, client: LLMClient, llm: LLMConfig, cfg: TextConfig, conn: Connection, now: datetime
    ) -> None:
        self.client, self.llm, self.cfg, self.conn, self.now = client, llm, cfg, conn, now
        self.system = (PROMPTS_DIR / f"{cfg.prompt_version}.md").read_text(encoding="utf-8")
        self.loan_system = (PROMPTS_DIR / "loan_book_v1.md").read_text(encoding="utf-8")

    def _call(
        self, spec: ModelSpec, task: str, system: str, user: str, schema: dict[str, Any],
        model: type[BaseModel],
    ) -> Any:  # fmt: skip
        raw = cached_call(
            self.conn, self.client, spec, task=task, prompt_version=self.cfg.prompt_version,
            system=system, user=user, schema=schema, now=self.now,
        )  # fmt: skip
        if raw is None:
            return None
        try:
            return model.model_validate(raw)
        except ValidationError:
            return None

    def values(
        self,
        doc: DocText,
        company: str,
        wanted: dict[str, str],  # metric code -> definition
        keyword_groups: list[list[str]],
        dual: bool,
    ) -> tuple[str, TextValues | None, TextValues | None]:
        """(source text sent, extractor output, tie-breaker output if `dual`)."""
        idx = select_chunks(
            doc.chunks, keyword_groups, self.cfg.max_chars, keep_first=doc.doc_role == "EX-99.1"
        )
        if not idx:
            return "", None, None
        source = "\n".join(doc.chunks[i] for i in idx)
        metrics = "\n".join(f"- {code}: {d}" for code, d in sorted(wanted.items()))
        user = (
            f"Company: {company}\nDocument: {doc.label} (accession {doc.accession_no})\n\n"
            f"Metrics to extract:\n{metrics}\n\n<excerpt>\n{source}\n</excerpt>"
        )
        first = self._call(self.llm.extractor, "text_metrics", self.system, user,
                           VALUES_SCHEMA, TextValues)  # fmt: skip
        second = (
            self._call(
                self.llm.tiebreaker, "text_metrics", self.system, user, VALUES_SCHEMA, TextValues
            )
            if dual
            else None
        )
        return source, first, second

    def loan_book(
        self, doc: DocText, company: str, keywords: list[str], definition: str
    ) -> tuple[str, LoanBook | None, LoanBook | None]:
        idx = select_chunks(doc.chunks, [keywords], self.cfg.max_chars, per_group=6)
        if not idx:
            return "", None, None
        source = "\n".join(doc.chunks[i] for i in idx)
        user = (
            f"Company: {company}\nDocument: {doc.label} (accession {doc.accession_no})\n\n"
            f"Definition: {definition}\n\n<excerpt>\n{source}\n</excerpt>"
        )
        out = [
            self._call(spec, "loan_book", self.loan_system, user, LOAN_SCHEMA, LoanBook)
            for spec in (self.llm.extractor, self.llm.tiebreaker)
        ]
        return source, out[0], out[1]


def _xbrl_values(conn: Connection, company_id: int) -> dict[tuple[str, str, int, int], float]:
    rows = conn.execute(
        select(metric_value.c.metric_code, metric_value.c.period_type,
               metric_value.c.fiscal_year, metric_value.c.fiscal_quarter, metric_value.c.value)
        .where(metric_value.c.company_id == company_id,
               metric_value.c.derivation.in_(RULE_DERIVATIONS))
    )  # fmt: skip
    return {(c, p, fy, fq): v for c, p, fy, fq, v in rows if v is not None}


def process_company(
    cfg: CompanyConfig,
    registry: Registry,
    industry: IndustryConfig,
    text_cfg: TextConfig,
    client: LLMClient,
    llm: LLMConfig,
    storage: Storage,
    conn: Connection,
    now: datetime,
) -> Stats:
    row = company_row(conn, cfg)
    company_id = row["company_id"]
    group = industry.groups[row["industry_group"]]
    creg = registry.for_company(cfg.ticker, group)
    text_metrics: dict[str, MetricDef] = {m.code: m for m in creg.metrics if m.kind == "text"}
    listed = {m.code: m for m in creg.metrics}
    prelim = {c: d for c, d in text_cfg.preliminary.items() if c in listed}
    cal: FiscalCalendar = learn_calendar(read_facts(conn, company_id), row["fiscal_year_end"])
    stats = Stats()
    ex = Extractor(client, llm, text_cfg, conn, now)
    company = row["legal_name"]

    docs = _mdna_docs(conn, company_id, text_cfg.sections) if text_metrics else []
    if text_cfg.earnings_release:
        docs += _release_docs(conn, company_id, storage)
    start = cfg.history_start.isoformat() if cfg.history_start else ""

    found: list[tuple[DocText, Candidate]] = []
    for doc in sorted(docs, key=lambda d: (d.filed_at, d.accession_no)):
        wanted = {c: m.extraction.definition for c, m in text_metrics.items() if m.extraction}
        groups = [m.extraction.keywords for m in text_metrics.values() if m.extraction]
        if doc.doc_role == "EX-99.1":
            wanted |= prelim
            groups.append(PRELIMINARY_KEYWORDS)
        if not wanted:
            continue
        dual_codes = {
            c for c, m in text_metrics.items() if m.extraction and m.extraction.dual_check
        }
        source, first, second = ex.values(doc, company, wanted, groups, bool(dual_codes))
        if first is None:
            continue
        stats.documents += 1
        convert = text_metrics | {c: listed[c] for c in prelim}
        got, rejected = candidates(first.values, convert, source, cal)
        stats.rejected += rejected
        if dual_codes:
            single = [c for c in got if c.metric_code not in dual_codes]
            double = [c for c in got if c.metric_code in dual_codes]
            other = candidates(second.values, convert, source, cal)[0] if second else []
            checked, dropped = dual_checked(double, other)
            stats.dual_dropped += dropped
            got = single + checked
        found += [(doc, c) for c in got if c.period_end >= start]

    # One value per (metric, period): the latest-filed document wins; disagreements are counted.
    chosen: dict[tuple[str, tuple[str, int, int]], tuple[DocText, Candidate]] = {}
    for doc, c in found:  # documents were processed oldest first
        slot = (c.metric_code, c.key)
        if slot in chosen and not agree(chosen[slot][1].value, c.value):
            stats.conflicts += 1
        chosen[slot] = (doc, c)

    xbrl = _xbrl_values(conn, company_id)
    out: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for (code, key), (doc, c) in chosen.items():
        derivation = "LLM_EXTRACTED"
        if code in prelim:
            if doc.doc_role != "EX-99.1":
                continue
            reported = xbrl.get((code, *key))
            if reported is not None:
                checks.append(
                    {"company_id": company_id, "accession_no": doc.accession_no,
                     "metric_code": code, "period_type": key[0], "fiscal_year": key[1],
                     "fiscal_quarter": key[2], "release_value": c.value,
                     "xbrl_value": reported, "agrees": agree(reported, c.value),
                     "checked_at": now}
                )  # fmt: skip
                if agree(reported, c.value):
                    stats.crosscheck_ok += 1
                else:
                    stats.crosscheck_mismatch += 1
                    stats.mismatches.append(
                        f"{code} {key}: release {c.value:,.0f} vs XBRL {reported:,.0f}"
                    )
                continue
            derivation = "PRELIMINARY"
            stats.preliminary += 1
        stats.extracted += 1
        out.append(
            {
                "company_id": company_id, "metric_code": code, "period_type": key[0],
                "fiscal_year": key[1], "fiscal_quarter": key[2], "period_end": c.period_end,
                "value": c.value, "value_status": "OK", "unit": listed[code].unit,
                "derivation": derivation, "formula": f"llm:{doc.doc_role}",
                "source_tags": json.dumps({"document": doc.label}),
                "source_accn": doc.accession_no, "evidence": c.evidence[:1000],
                "computed_at": now,
            }
        )  # fmt: skip
    keys = ["company_id", "metric_code", "period_type", "fiscal_year", "fiscal_quarter"]
    upsert(conn, metric_value, out, key_cols=keys)
    upsert(conn, release_check, checks,
           key_cols=["company_id", "accession_no", *keys[1:]])  # fmt: skip
    if stats.extracted or stats.rejected:
        conn.execute(
            insert(llm_task_stat).values(
                company_id=company_id, task="text_metrics",
                generated=stats.extracted + stats.rejected, kept=stats.extracted, created_at=now,
            )
        )  # fmt: skip
    produced = {tuple(r[k] for k in keys[1:]) for r in out}
    existing = conn.execute(
        select(*(metric_value.c[k] for k in keys[1:])).where(
            metric_value.c.company_id == company_id,
            metric_value.c.derivation.in_(LLM_DERIVATIONS),
        )
    ).all()
    stale = [tuple(e) for e in existing if tuple(e) not in produced]
    if stale:
        conn.execute(
            delete(metric_value).where(
                metric_value.c.company_id == company_id,
                metric_value.c.derivation.in_(LLM_DERIVATIONS),
                tuple_(*(metric_value.c[k] for k in keys[1:])).in_(stale),
            )
        )

    if group.code in text_cfg.loan_portfolio.groups:
        stats.loan_segments = _loan_book(conn, ex, company_id, company, text_cfg, docs, now)
    return stats


def _loan_book(
    conn: Connection,
    ex: Extractor,
    company_id: int,
    company: str,
    text_cfg: TextConfig,
    docs: Sequence[DocText],
    now: datetime,
) -> int:
    """Loan book from the latest periodic report's MD&A; both models must agree."""
    periodic = [d for d in docs if d.doc_role == "PRIMARY"]
    if not periodic:
        return 0
    doc = max(periodic, key=lambda d: (d.filed_at, d.accession_no))
    lp = text_cfg.loan_portfolio
    source, first, second = ex.loan_book(doc, company, lp.keywords, lp.definition)
    if first is None or second is None:
        return 0
    rows_a, _ = loan_rows(first.segments, source)
    rows_b, _ = loan_rows(second.segments, source)
    other = {(r.segment.lower(), r.period_end): r for r in rows_b}
    agreed = [
        r
        for r in rows_a
        if (m := other.get((r.segment.lower(), r.period_end))) is not None
        and agree(r.outstanding, m.outstanding)
    ]
    if not agreed:
        return 0
    latest = max(r.period_end for r in agreed)
    agreed = [r for r in agreed if r.period_end == latest]
    total = sum(r.outstanding for r in agreed)
    rows = [
        {
            "company_id": company_id,
            "period_end": r.period_end,
            "segment": r.segment,
            "outstanding": r.outstanding,
            "pct_of_book": r.outstanding / total if total else None,
            "nonaccrual": r.nonaccrual,
            "evidence": r.evidence[:1000],
            "source_doc_id": doc.doc_id,
            "updated_at": now,
        }
        for r in agreed
    ]
    upsert(conn, loan_portfolio_segment, rows, key_cols=["company_id", "period_end", "segment"])
    conn.execute(
        delete(loan_portfolio_segment).where(
            and_(
                loan_portfolio_segment.c.company_id == company_id,
                loan_portfolio_segment.c.period_end != latest,
            )
        )
    )
    return len(rows)


def run_text_metrics_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    text_cfg: TextConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    storage: Storage,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    if llm is None:
        return [TaskResult(c.ticker, "textmetrics", ok=False, detail="LLM not configured")
                for c in companies]  # fmt: skip
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                stats = process_company(
                    cfg, registry, industry, text_cfg, llm[0], llm[1], storage, conn, now()
                )
            for m in stats.mismatches:
                log.warning("%s: earnings release vs XBRL mismatch: %s", cfg.ticker, m)
            log.info("%s: textmetrics %s", cfg.ticker,
                     {k: v for k, v in stats.__dict__.items() if k != "mismatches"})  # fmt: skip
            detail = f"{stats.extracted} values, {stats.preliminary} preliminary"
            results.append(TaskResult(cfg.ticker, "textmetrics", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: textmetrics stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "textmetrics", ok=False, detail=str(exc)))
    return results
