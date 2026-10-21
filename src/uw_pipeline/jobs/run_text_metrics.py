"""Phase 5a `textmetrics` stage (decisions D-032).

- Text metrics (`kind: text` in metrics.yaml: RevPAR, NIM, ...) from MD&A chunks and earnings
  releases -> metric_value, derivation LLM_EXTRACTED. Formula metrics with an `extraction`
  (bank capital ratios, D-042) are extracted too, but kept only for periods XBRL does not cover;
  elsewhere they are cross-checked against XBRL (release_check).
- Identities (config/dq_rules.yaml, D-042): text values that break one (RevPAR = ADR x
  occupancy, CET1 <= Tier 1 <= total) are dropped, and a loan book adding up to more than total
  loans is not stored.
- Preliminary results from earnings releases (decisions D-022) -> metric_value, derivation
  PRELIMINARY, only for periods without an XBRL value. Where XBRL exists, the release value is
  cross-checked against it instead.
- Bank loan book by loan class, latest period and prior year-end -> loan_portfolio_segment,
  plus the rule-generated Loan Portfolio modal content -> loan_portfolio_detail (Phase 8).
Bank metrics (`dual_check`) and the loan book are kept only where the extractor and the
tie-breaker model (config/llm.yaml) extract the same number independently.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine, insert, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.storage import Storage
from uw_pipeline.compute.registry import MetricDef, Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, ModelSpec, cached_call
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.enrich.schemas import (
    ExtractedValue,
    LoanBook,
    LoanTable,
    TableValues,
    TextValues,
    strict_json_schema,
)
from uw_pipeline.enrich.sources import SourceLabels, SourceResolver
from uw_pipeline.enrich.table_reader import (
    NONACCRUAL_HEADERS,
    check_cell,
    check_loan,
    could_pass,
    relevant,
)
from uw_pipeline.enrich.text_metrics import (
    Candidate,
    LoanRow,
    TextConfig,
    agree,
    candidates,
    dual_checked,
    loan_key,
    matches_release,
    select_chunks,
)
from uw_pipeline.enrich.untrusted import data_block
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import RULE_DERIVATIONS, company_row, read_facts
from uw_pipeline.load.upsert import delete_keys, upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.periods import FiscalCalendar, learn_calendar
from uw_pipeline.normalize.sections import chunk
from uw_pipeline.normalize.table_grid import tables
from uw_pipeline.quality.identities import IDENTITY_TASK, Identities
from uw_pipeline.schema import (
    filing_section,
    llm_task_stat,
    loan_portfolio_detail,
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
TABLE_SCHEMA = strict_json_schema(TableValues)
LOAN_TABLE_SCHEMA = strict_json_schema(LoanTable)


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
    table_rejected: int = 0  # cells that failed the mechanical check (D-044)
    table_failed: bool = False
    identity_checked: int = 0
    identity_dropped: int = 0
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
    raw_path: str | None = None  # the document's HTML, for metrics read from tables (D-044)


def _mdna_docs(conn: Connection, company_id: int, codes: Sequence[str]) -> list[DocText]:
    rows = conn.execute(
        select(
            source_document.c.doc_id, source_document.c.accession_no, source_document.c.form_type,
            source_document.c.filed_at, filing_section.c.section_id,
            filing_section.c.section_code, source_document.c.raw_path,
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
                    f"{r.form_type} filed {r.filed_at}, section {r.section_code}", list(texts),
                    r.raw_path)
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
                    [c.text for c in chunk(text)], r["raw_path"])
        )  # fmt: skip
    return docs


class Extractor:
    def __init__(
        self, client: LLMClient, llm: LLMConfig, cfg: TextConfig, conn: Connection, now: datetime
    ) -> None:
        self.client, self.llm, self.cfg, self.conn, self.now = client, llm, cfg, conn, now
        self.first = (
            llm.extractor.model_copy(update={"effort": cfg.extractor_effort})
            if cfg.extractor_effort
            else llm.extractor
        )
        self.system = (PROMPTS_DIR / f"{cfg.prompt_version}.md").read_text(encoding="utf-8")
        self.table_version = cfg.table_prompt_version
        self.table_system = (PROMPTS_DIR / f"{self.table_version}.md").read_text(encoding="utf-8")
        self.loan_version = cfg.loan_portfolio.prompt_version
        self.loan_system = (PROMPTS_DIR / f"{self.loan_version}.md").read_text(encoding="utf-8")

    def _call(
        self, spec: ModelSpec, task: str, system: str, user: str, schema: dict[str, Any],
        model: type[BaseModel], prompt_version: str | None = None,
    ) -> Any:  # fmt: skip
        raw = cached_call(
            self.conn, self.client, spec, task=task,
            prompt_version=prompt_version or self.cfg.prompt_version,
            system=system, user=user, schema=schema, now=self.now,
            validate=model.model_validate,
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
            f"Metrics to extract:\n{metrics}\n\n{data_block('excerpt', source)}"
        )
        first = self._call(self.first, "text_metrics", self.system, user,
                           VALUES_SCHEMA, TextValues)  # fmt: skip
        second = (
            self._call(
                self.llm.tiebreaker, "text_metrics", self.system, user, VALUES_SCHEMA, TextValues
            )
            if dual
            else None
        )
        return source, first, second

    def table_values(
        self,
        doc: DocText,
        company: str,
        metrics: dict[str, MetricDef],
        storage: Storage,
        dual: bool,
    ) -> tuple[bool, list[ExtractedValue] | None, list[ExtractedValue] | None, int]:
        """Metrics read from the document's tables by cell (D-044): (tables sent, extractor's
        accepted values, tie-breaker's if `dual`, rejected cells). None: the call failed."""
        if doc.raw_path is None:
            return False, [], [], 0
        specs = [sp for m in metrics.values() if m.extraction and (sp := m.extraction.table)]
        terms = sorted({t for sp in specs for t in (sp.find or sp.column)})
        grid = [t for t in tables(storage.read_bytes(doc.raw_path))
                if any(could_pass(t, sp) for sp in specs)]  # fmt: skip
        chosen = relevant(grid, terms, self.cfg.max_chars)
        if not chosen:
            return False, [], [], 0
        by_id = {f"T{t.index}": t for t in chosen}
        defs = "\n".join(f"- {c}: {m.extraction.definition}"
                         for c, m in sorted(metrics.items()) if m.extraction)  # fmt: skip
        listing = "\n\n".join(t.render() for t in chosen)
        user = (
            f"Company: {company}\nDocument: {doc.label} (accession {doc.accession_no})\n\n"
            f"Metrics to read:\n{defs}\n\n{data_block('tables', listing)}"
        )
        rejected = 0

        def read(spec: ModelSpec) -> list[ExtractedValue] | None:
            nonlocal rejected
            out = self._call(spec, "table_metrics", self.table_system, user, TABLE_SCHEMA,
                             TableValues, self.table_version)  # fmt: skip
            if out is None:
                return None
            kept = []
            for cell in out.values:
                m = metrics.get(cell.metric_code)
                table_spec = m.extraction.table if m and m.extraction else None
                checked = check_cell(by_id, cell, table_spec) if table_spec else None
                if checked is None or checked.value is None:
                    rejected += 1
                    log.info("%s %s %s r%s c%s withheld: %s", doc.label, cell.metric_code,
                             cell.table, cell.row, cell.col,
                             checked.reason if checked else "not requested")  # fmt: skip
                    continue
                kept.append(checked.value)
            return kept

        first = read(self.first)
        second = read(self.llm.tiebreaker) if dual else []
        return True, first, second, rejected

    def loan_book(
        self, doc: DocText, company: str, keywords: list[str], definition: str, storage: Storage
    ) -> tuple[list[LoanRow] | None, list[LoanRow] | None]:
        """Loan classes read by cell from the credit-portfolio tables (D-044), by both models;
        None where a model's call failed or the document has no such table."""
        if doc.raw_path is None:
            return None, None
        # Credit-portfolio tables: loans and, in the same table, nonaccrual / nonperforming.
        grid = [t for t in tables(storage.read_bytes(doc.raw_path))
                if any(w in t.render().lower() for w in NONACCRUAL_HEADERS)]  # fmt: skip
        chosen = relevant(grid, keywords, self.cfg.max_chars)
        if not chosen:
            return None, None
        by_id = {f"T{t.index}": t for t in chosen}
        listing = "\n\n".join(t.render() for t in chosen)
        user = (
            f"Company: {company}\nDocument: {doc.label} (accession {doc.accession_no})\n\n"
            f"Definition: {definition}\n\n{data_block('tables', listing)}"
        )

        def read(spec: ModelSpec) -> list[LoanRow] | None:
            out = self._call(spec, "loan_book", self.loan_system, user, LOAN_TABLE_SCHEMA,
                             LoanTable, self.loan_version)  # fmt: skip
            if out is None:
                return None
            rows = []
            for cell in out.segments:
                row, reason = check_loan(by_id, cell)
                if row is None:
                    log.info("%s loan %r %s r%s withheld: %s", doc.label, cell.segment,
                             cell.table, cell.row, reason)  # fmt: skip
                else:
                    rows.append(row)
            return rows

        return read(self.first), read(self.llm.tiebreaker)


def _xbrl_values(
    conn: Connection, company_id: int
) -> dict[tuple[str, str, int, int], float | None]:
    """Rule-derived values by key, including N/M rows (value None): a period that XBRL already
    covers never gets a PRELIMINARY value, which would alternate with compute's row."""
    rows = conn.execute(
        select(metric_value.c.metric_code, metric_value.c.period_type,
               metric_value.c.fiscal_year, metric_value.c.fiscal_quarter, metric_value.c.value)
        .where(metric_value.c.company_id == company_id,
               metric_value.c.derivation.in_(RULE_DERIVATIONS))
    )  # fmt: skip
    return {(c, p, fy, fq): v for c, p, fy, fq, v in rows}


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
    labels: SourceLabels | None = None,
    identities: Identities | None = None,
) -> Stats:
    identities = identities or Identities()
    row = company_row(conn, cfg)
    company_id = row["company_id"]
    group = industry.group_of(row["industry_group"], cfg)
    creg = registry.for_company(cfg.ticker, group)
    text_metrics: dict[str, MetricDef] = {m.code: m for m in creg.metrics if m.extraction}
    listed = {m.code: m for m in creg.metrics}
    prelim = {c: d for c, d in text_cfg.preliminary.items() if c in listed}
    cal: FiscalCalendar = learn_calendar(
        read_facts(conn, company_id), row["fiscal_year_end"] or "1231"
    )  # same fallback as the compute stage
    stats = Stats()
    ex = Extractor(client, llm, text_cfg, conn, now)
    company = row["legal_name"]

    docs = _mdna_docs(conn, company_id, text_cfg.sections) if text_metrics else []
    if text_cfg.earnings_release:
        docs += _release_docs(conn, company_id, storage)
    start = cfg.history_start.isoformat() if cfg.history_start else ""

    found: list[tuple[DocText, Candidate]] = []
    # A document whose LLM call failed this run (not one without relevant text) must not make
    # its earlier values look stale: they would vanish from the app until a later run succeeds.
    incomplete = False
    table_metrics = {c: m for c, m in text_metrics.items() if m.extraction and m.extraction.table}
    quote_metrics = {c: m for c, m in text_metrics.items() if c not in table_metrics}
    for doc in sorted(docs, key=lambda d: (d.filed_at, d.accession_no)):
        if table_metrics:
            found += _table_candidates(doc, company, table_metrics, ex, storage, cal, stats,
                                       start)  # fmt: skip
            incomplete = incomplete or stats.table_failed
        wanted = {c: m.extraction.definition for c, m in quote_metrics.items() if m.extraction}
        groups = [m.extraction.keywords for m in quote_metrics.values() if m.extraction]
        if doc.doc_role == "EX-99.1":
            wanted |= prelim
            groups.append(PRELIMINARY_KEYWORDS)
        if not wanted:
            continue
        dual_codes = {
            c for c, m in quote_metrics.items() if m.extraction and m.extraction.dual_check
        }
        source, first, second = ex.values(doc, company, wanted, groups, bool(dual_codes))
        if source and (first is None or (dual_codes and second is None)):
            incomplete = True
        if first is None:
            continue
        stats.documents += 1
        # Only what this path asked for: a table metric answered from a quote would skip
        # its cell check (D-044).
        convert = quote_metrics | {c: listed[c] for c in prelim}
        got, rejected = candidates(first.values, convert, source, cal)
        stats.rejected += rejected
        if dual_codes:
            single = [c for c in got if c.metric_code not in dual_codes]
            double = [c for c in got if c.metric_code in dual_codes]
            other = candidates(second.values, convert, source, cal)[0] if second else []
            checked, dropped = dual_checked(double, other, doc.label)
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
        # XBRL-backed: preliminary release figures, and formula metrics with a text fallback.
        if code in prelim or listed[code].kind != "text":
            if code in prelim and doc.doc_role != "EX-99.1":
                continue
            if (code, *key) in xbrl:
                reported = xbrl[(code, *key)]
                if reported is None:
                    continue  # XBRL has the period as N/M: nothing to compare, nothing to add
                checks.append(
                    {"company_id": company_id, "accession_no": doc.accession_no,
                     "metric_code": code, "period_type": key[0], "fiscal_year": key[1],
                     "fiscal_quarter": key[2], "release_value": c.value,
                     "xbrl_value": reported, "agrees": matches_release(reported, c),
                     "checked_at": now}
                )  # fmt: skip
                if matches_release(reported, c):
                    stats.crosscheck_ok += 1
                else:
                    stats.crosscheck_mismatch += 1
                    stats.mismatches.append(
                        f"{code} {key}: text {c.value:,.4g} vs XBRL {reported:,.4g}"
                    )
                continue
            if doc.doc_role == "EX-99.1":
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
    out = _identity_gate(out, xbrl, identities, stats, cfg.ticker)
    stats.extracted = len(out)
    if stats.identity_checked:
        conn.execute(insert(llm_task_stat).values(
            company_id=company_id, task=IDENTITY_TASK, generated=stats.identity_checked,
            kept=stats.identity_checked - stats.identity_dropped, created_at=now))  # fmt: skip
    keys = ["company_id", "metric_code", "period_type", "fiscal_year", "fiscal_quarter"]
    upsert(conn, metric_value, out, key_cols=keys)
    check_keys = ["accession_no", *keys[1:]]
    upsert(conn, release_check, checks, key_cols=["company_id", *check_keys])
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
    if stale and incomplete:
        log.warning("%s: an extraction failed this run; %d earlier values kept", cfg.ticker,
                    len(stale))  # fmt: skip
        stale = []
    if stale:
        delete_keys(conn, metric_value, keys[1:], stale,
                    metric_value.c.company_id == company_id,
                    metric_value.c.derivation.in_(LLM_DERIVATIONS))  # fmt: skip
    # Checks follow the current reading too (D-052): one this run no longer makes (the value
    # is read differently, from a later release, or XBRL has the period as N/M) is dropped.
    made = {tuple(r[k] for k in check_keys) for r in checks}
    old = [tuple(e) for e in conn.execute(
        select(*(release_check.c[k] for k in check_keys))
        .where(release_check.c.company_id == company_id)
    ).all() if tuple(e) not in made]  # fmt: skip
    if old and not incomplete:
        delete_keys(conn, release_check, check_keys, old,
                    release_check.c.company_id == company_id)  # fmt: skip

    if group.code in text_cfg.loan_portfolio.groups:
        resolver = SourceResolver(conn, row, labels) if labels else None
        stats.loan_segments = _loan_book(conn, ex, company_id, company, text_cfg, docs, now,
                                         resolver, identities, storage)  # fmt: skip
    return stats


def _table_candidates(
    doc: DocText, company: str, metrics: dict[str, MetricDef], ex: Extractor, storage: Storage,
    cal: FiscalCalendar, stats: Stats, start: str,
) -> list[tuple[DocText, Candidate]]:  # fmt: skip
    dual = any(m.extraction and m.extraction.dual_check for m in metrics.values())
    sent, first, second, rejected = ex.table_values(doc, company, metrics, storage, dual)
    stats.table_rejected += rejected
    stats.table_failed = sent and (first is None or (dual and second is None))
    if not first:
        return []
    stats.documents += 1
    got, bad = candidates(first, metrics, "\n".join(v.evidence for v in first), cal)
    stats.rejected += bad
    if dual:
        other = candidates(second or [], metrics, "\n".join(v.evidence for v in second or []),
                           cal)[0]  # fmt: skip
        dual_codes = {c for c, m in metrics.items() if m.extraction and m.extraction.dual_check}
        single = [c for c in got if c.metric_code not in dual_codes]
        checked, dropped = dual_checked(
            [c for c in got if c.metric_code in dual_codes], other, doc.label
        )
        stats.dual_dropped += dropped
        got = single + checked
    return [(doc, c) for c in got if c.period_end >= start]


def _identity_gate(
    rows: list[dict[str, Any]],
    xbrl: dict[tuple[str, str, int, int], float | None],
    identities: Identities,
    stats: Stats,
    ticker: str,
) -> list[dict[str, Any]]:
    """Drop text values of a period where they, with the XBRL values, break an identity."""
    text: dict[tuple[str, int, int], dict[str, float]] = {}
    for r in rows:
        text.setdefault((r["period_type"], r["fiscal_year"], r["fiscal_quarter"]), {})[
            r["metric_code"]] = r["value"]  # fmt: skip
    drop: set[tuple[str, str, int, int]] = set()
    for key, mine in text.items():
        for ident in identities.checks:
            involved = ident.metrics()
            if not involved & mine.keys():
                continue
            values = {code: v for (code, *k), v in xbrl.items()
                      if tuple(k) == key and code in involved and v is not None}  # fmt: skip
            values |= {code: v for code, v in mine.items() if code in involved}
            if not involved <= values.keys():
                continue
            stats.identity_checked += 1
            problem = ident.check(values, key[0])
            if problem:
                stats.identity_dropped += 1
                log.warning("%s %s %s: %s; text values dropped", ticker, ident.name, key, problem)
                drop |= {(code, *key) for code in involved & mine.keys()}
    return [r for r in rows if (r["metric_code"], r["period_type"], r["fiscal_year"],
                                r["fiscal_quarter"]) not in drop]  # fmt: skip


def _rolled_up(
    rows_a: list[LoanRow], rows_b: list[LoanRow], agreed: dict[tuple[str, str], LoanRow]
) -> dict[tuple[str, str], LoanRow]:
    """Where the models read a segment at different levels (one gives the segment's single
    retained-loan line, the other its classes) and both add up to the same amount, the single
    line: its value is confirmed by both models; the classes only by one."""

    def groups(rows: list[LoanRow]) -> dict[tuple[str, str], list[LoanRow]]:
        out: dict[tuple[str, str], list[LoanRow]] = {}
        for r in rows:
            if (loan_key(r.segment), r.period_end) not in agreed and r.parent_segment:
                out.setdefault((loan_key(r.parent_segment), r.period_end), []).append(r)
        return out

    ga, gb = groups(rows_a), groups(rows_b)
    kept: dict[tuple[str, str], LoanRow] = {}
    for key in ga.keys() & gb.keys():
        one, many = sorted((ga[key], gb[key]), key=len)
        if (
            len(one) == 1
            and len(many) > 1
            and agree(one[0].outstanding, sum(r.outstanding for r in many))
        ):
            kept[(loan_key(one[0].segment), one[0].period_end)] = one[0]
    return kept


def _loan_book(
    conn: Connection,
    ex: Extractor,
    company_id: int,
    company: str,
    text_cfg: TextConfig,
    docs: Sequence[DocText],
    now: datetime,
    resolver: SourceResolver | None,
    identities: Identities | None = None,
    storage: Storage | None = None,
) -> int:
    """Loan book by class from the latest periodic report's MD&A, for the latest period and the
    prior year-end shown with it; both models must agree on each row."""
    periodic = [d for d in docs if d.doc_role == "PRIMARY"]
    if not periodic or storage is None:
        return 0
    doc = max(periodic, key=lambda d: (d.filed_at, d.accession_no))
    lp = text_cfg.loan_portfolio
    rows_a, rows_b = ex.loan_book(doc, company, lp.keywords, lp.definition, storage)
    if rows_a is None or rows_b is None:
        return 0
    other = {(loan_key(r.segment), r.period_end): r for r in rows_b}
    agreed: dict[tuple[str, str], LoanRow] = {}
    for r in rows_a:
        m = other.get((loan_key(r.segment), r.period_end))
        if m is not None and agree(r.outstanding, m.outstanding):
            agreed.setdefault((loan_key(r.segment), r.period_end), r)
    agreed |= _rolled_up(rows_a, rows_b, agreed)
    if not agreed:
        return 0
    periods = sorted({p for _, p in agreed}, reverse=True)
    latest = periods[0]
    # The comparison column: the latest earlier period shown (the prior fiscal year-end).
    prior = periods[1] if len(periods) > 1 else None
    keep = [r for r in agreed.values() if r.period_end in (latest, prior)]
    totals = {p: sum(r.outstanding for r in keep if r.period_end == p) for p in (latest, prior)}
    check = (identities or Identities()).loan_book
    for p, total in totals.items():
        reported = conn.execute(
            select(metric_value.c.value).where(
                metric_value.c.company_id == company_id, metric_value.c.metric_code == check.total,
                metric_value.c.period_end == p, metric_value.c.derivation.in_(RULE_DERIVATIONS))
        ).scalar()  # fmt: skip
        if reported and total > reported * (1 + check.max_over):
            log.warning("loan book %s: classes add up to %s, more than total loans %s; "
                        "not stored", p, f"{total:,.0f}", f"{reported:,.0f}")  # fmt: skip
            conn.execute(insert(llm_task_stat).values(
                company_id=company_id, task=IDENTITY_TASK, generated=1, kept=0,
                created_at=now))  # fmt: skip
            return 0
    rows = [
        {
            "company_id": company_id,
            "period_end": r.period_end,
            "segment": r.segment[:200],
            "parent_segment": r.parent_segment[:200] or None,
            "outstanding": r.outstanding,
            "pct_of_book": r.outstanding / totals[r.period_end] if totals[r.period_end] else None,
            "nonaccrual": r.nonaccrual,
            "nonaccrual_ratio": r.nonaccrual / r.outstanding
            if r.nonaccrual is not None and r.outstanding
            else None,
            "evidence": r.evidence[:1000],
            "source_doc_id": doc.doc_id,
            "updated_at": now,
        }
        for r in keep
    ]
    # updated_at changes only when a row's content does (it is shown as "Updated At").
    seg = loan_portfolio_segment.c
    before = {
        (r.period_end, r.segment): r
        for r in conn.execute(select(loan_portfolio_segment).where(seg.company_id == company_id))
    }
    content = ("outstanding", "nonaccrual", "parent_segment", "evidence", "source_doc_id")
    for row in rows:
        old = before.get((row["period_end"], row["segment"]))
        if old is not None and all(getattr(old, k) == row[k] for k in content):
            row["updated_at"] = old.updated_at
    upsert(conn, loan_portfolio_segment, rows, key_cols=["company_id", "period_end", "segment"])
    current = {(r["period_end"], r["segment"]) for r in rows}
    stale = [
        (pe, seg)
        for pe, seg in conn.execute(
            select(loan_portfolio_segment.c.period_end, loan_portfolio_segment.c.segment).where(
                loan_portfolio_segment.c.company_id == company_id
            )
        ).all()
        if (pe, seg) not in current
    ]
    if stale:
        delete_keys(conn, loan_portfolio_segment, ["period_end", "segment"], stale,
                    loan_portfolio_segment.c.company_id == company_id)  # fmt: skip
    if resolver is not None:
        detail = loan_detail(rows, latest, prior, doc, resolver)
        old_d = conn.execute(
            select(loan_portfolio_detail)
            .where(loan_portfolio_detail.c.company_id == company_id)
        ).mappings().first()  # fmt: skip
        same = old_d is not None and all(old_d[k] == v for k, v in detail.items())
        stamp = old_d["updated_at"] if same and old_d is not None else now
        upsert(conn, loan_portfolio_detail,
               [detail | {"company_id": company_id, "updated_at": stamp}],
               key_cols=["company_id"])  # fmt: skip
    return len(rows)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def loan_detail(
    rows: Sequence[dict[str, Any]],
    latest: str,
    prior: str | None,
    doc: DocText,
    resolver: SourceResolver,
) -> dict[str, Any]:
    """Left side of the Loan Portfolio Composition modal, by rules (Phase 8)."""
    section = "10K-7" if doc.form_type.startswith("10-K") else "10Q-I-2"
    ref = resolver.document(doc.doc_id, section)
    where = f"{doc.form_type} filed {doc.filed_at} ({resolver.section_label(section)})"
    compare = f", compared with {prior}" if prior else ""
    methodology = (
        f"Retained loans by loan class as of {latest}{compare}, from the credit portfolio "
        f"tables in the {where}. Loans held-for-sale and loans at fair value are excluded. "
        "Each balance and nonaccrual amount is read from the table by two models "
        "independently, by cell; a cell is kept only where its row names the loan class, its "
        "column header the balance or nonaccrual amount and the date, and both models agree. "
        "Share of book = class balance / sum of the classes shown; nonaccrual "
        "ratio = nonaccrual loans / class balance (credit card loans are generally exempt from "
        "nonaccrual status, so no ratio is shown for them)."
    )
    sources = [{"title": ref.title, "url": ref.url, "date": ref.date}] if ref else []
    cur = [r for r in rows if r["period_end"] == latest]
    old = {r["segment"]: r for r in rows if r["period_end"] == prior}
    points: list[dict[str, str]] = []
    for r in sorted(cur, key=lambda r: -r["outstanding"]):
        parts = [f"{fmt_value(r['outstanding'], 'USD')} ({_pct(r['pct_of_book'] or 0)} of book)"]
        if r["nonaccrual"] is not None:
            parts.append(f"nonaccrual {fmt_value(r['nonaccrual'], 'USD')}"
                         f" ({_pct(r['nonaccrual_ratio'] or 0)})")  # fmt: skip
        p = old.get(r["segment"])
        if p is not None and p["outstanding"]:
            change = r["outstanding"] / p["outstanding"] - 1
            parts.append(f"{change:+.1%} vs {prior}")
        points.append({"label": r["segment"], "text": "; ".join(parts),
                       "evidence": r["evidence"]})  # fmt: skip
    total = sum(r["outstanding"] for r in cur)
    summary = f"Total of the classes shown: {fmt_value(total, 'USD')} as of {latest}"
    if old:
        before = sum(p["outstanding"] for p in old.values())
        if before:
            summary += f" ({total / before - 1:+.1%} vs {fmt_value(before, 'USD')} at {prior})"
    disclosed = [r for r in cur if r["nonaccrual"] is not None]
    if disclosed:
        na = sum(r["nonaccrual"] for r in disclosed)
        base = sum(r["outstanding"] for r in disclosed)
        summary += (f"; nonaccrual {fmt_value(na, 'USD')}, {_pct(na / base)} of the classes "
                    "that disclose it")  # fmt: skip
    points.insert(0, {"label": "Total", "text": summary, "evidence": ""})
    return {
        "period_end": latest,
        "prior_period_end": prior,
        "methodology": methodology,
        "data_sources_json": json.dumps(sources),
        "raw_data_points_json": json.dumps(points),
    }


def run_text_metrics_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    text_cfg: TextConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    storage: Storage,
    engine: Engine,
    *,
    labels: SourceLabels | None = None,
    identities: Identities | None = None,
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
                    cfg,
                    registry,
                    industry,
                    text_cfg,
                    llm[0],
                    llm[1],
                    storage,
                    conn,
                    now(),
                    labels,
                    identities,
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
