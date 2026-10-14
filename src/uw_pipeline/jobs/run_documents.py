"""Phase 3 `documents` stage: register filings, download text documents, split sections.

- Every 10-K/10-Q/8-K (and amendment) filed within `event_lookback_days` is registered in
  `source_document`, metadata only, so signals can cite it.
- Original 10-K/10-Qs within `text_lookback_days` are downloaded and split into
  `filing_section` + `section_chunk`. Amendments are registered but not split: they are often
  Part III-only, e.g. LUV's and SKYD's FY2025 10-K/As.
- 8-Ks with items in `fetch_body_items` are downloaded and split by item.
- The EX-99.1 earnings release of every Item 2.02 8-K within `text_lookback_days` is downloaded
  for the Phase 5 preliminary-results extraction (decisions D-022).
- For industry groups with `mdna_history_years`, older 10-Ks in that window are downloaded too,
  keeping only their MD&A, for text-metric history (Phase 8, decisions D-036). With no other
  sections stored they add no signal votes and no insight excerpts.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, select, update

from uw_pipeline.clients.sec import SecClient, archive_folder_url
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig
from uw_pipeline.extract.filings import Filing, exhibit_by_type, filings_since
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import insert_missing, upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.sections import (
    Section,
    apply_rules,
    audit_report,
    chunk,
    split_8k_items,
    split_items,
)
from uw_pipeline.schema import filing_section, section_chunk, source_document
from uw_pipeline.signals.rules import SignalRules

log = logging.getLogger(__name__)

TEXT_FORMS = ("10-K", "10-Q")
EVENT_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A")
# Sections that must be found; a miss is logged (PLAN Phase 3 acceptance).
HISTORY_SECTIONS = ("10K-7",)
REQUIRED_SECTIONS = {"10-K": ("10K-1A", "10K-3", "10K-7"), "10-Q": ("10Q-I-2",)}


def _doc_row(company_id: int, cik: str, f: Filing, name: str, role: str) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "source_type": "SEC_FILING",
        "form_type": f.form,
        "doc_role": role,
        "accession_no": f.accn,
        "doc_name": name,
        "items": f.items or None,
        "filed_at": f.filed,
        "period_end": f.report_date or None,
        "url": f"{archive_folder_url(cik, f.accn)}/{name}",
    }


def _doc_id(conn: Connection, accn: str, name: str) -> tuple[int, str | None]:
    row = conn.execute(
        select(source_document.c.doc_id, source_document.c.content_hash).where(
            and_(source_document.c.accession_no == accn, source_document.c.doc_name == name)
        )
    ).one()
    return int(row.doc_id), row.content_hash


def _store_sections(conn: Connection, doc_id: int, sections: list[Section]) -> None:
    for sec in sections:
        upsert(
            conn,
            filing_section,
            [
                {
                    "doc_id": doc_id,
                    "section_code": sec.code,
                    "start_offset": sec.start,
                    "char_count": len(sec.text),
                    "text": sec.text,
                    "text_hash": hashlib.sha256(sec.text.encode()).hexdigest(),
                }
            ],
            key_cols=["doc_id", "section_code"],
        )
        section_id = conn.execute(
            select(filing_section.c.section_id).where(
                and_(filing_section.c.doc_id == doc_id, filing_section.c.section_code == sec.code)
            )
        ).scalar_one()
        chunks = chunk(sec.text)
        upsert(
            conn,
            section_chunk,
            [
                {
                    "section_id": section_id,
                    "chunk_index": c.index,
                    "start_offset": c.start,
                    "end_offset": c.end,
                    "text": c.text,
                }
                for c in chunks
            ],
            key_cols=["section_id", "chunk_index"],
        )
        conn.execute(
            delete(section_chunk).where(
                section_chunk.c.section_id == section_id,
                section_chunk.c.chunk_index >= len(chunks),
            )
        )
    # Sections the new split no longer finds (e.g. after a `sections:` override fixed a false
    # match) go too, or their old text would keep feeding signals and insights.
    stale = (
        conn.execute(
            select(filing_section.c.section_id).where(
                filing_section.c.doc_id == doc_id,
                filing_section.c.section_code.not_in([s.code for s in sections]),
            )
        )
        .scalars()
        .all()
    )
    if stale:
        conn.execute(delete(section_chunk).where(section_chunk.c.section_id.in_(stale)))
        conn.execute(delete(filing_section).where(filing_section.c.section_id.in_(stale)))


def _download(
    conn: Connection,
    sec: SecClient,
    cik: str,
    accn: str,
    name: str,
    *,
    force: bool,
    now: datetime,
) -> tuple[int, bytes | None]:
    """Fetch a document (cached by accession). Returns (doc_id, bytes or None if unchanged)."""
    doc_id, known_hash = _doc_id(conn, accn, name)
    raw = sec.filing_document(cik, accn, name)
    if not force and known_hash == raw.content_hash:
        return doc_id, None
    conn.execute(
        update(source_document)
        .where(source_document.c.doc_id == doc_id)
        .values(content_hash=raw.content_hash, raw_path=raw.raw_path, fetched_at=now)
    )
    data: bytes = raw.payload
    return doc_id, data


def process_company(
    cfg: CompanyConfig,
    sec: SecClient,
    registry: Registry,
    rules: SignalRules,
    conn: Connection,
    *,
    today: date,
    text_lookback_days: int,
    event_lookback_days: int,
    force: bool,
    now: datetime,
    history_years: int = 0,
) -> dict[str, int]:
    row = company_row(conn, cfg)
    cik, company_id = row["cik"], row["company_id"]
    subs = sec.submissions(cik, force=force).payload
    since_events = today - timedelta(days=event_lookback_days)
    since_text = (today - timedelta(days=text_lookback_days)).isoformat()
    filings = filings_since(sec, cik, subs, since_events, EVENT_FORMS, force=force)
    if cfg.history_start:  # successor-only companies (decisions D-012)
        filings = [f for f in filings if f.filed >= cfg.history_start.isoformat()]

    insert_missing(
        conn,
        source_document,
        [_doc_row(company_id, cik, f, f.primary_doc, "PRIMARY") for f in filings],
        key_cols=["accession_no", "doc_name"],
    )
    stats = {"registered": len(filings), "downloaded": 0, "sections": 0, "missing_sections": 0}
    section_rules = {c: (r.start, r.end) for c, r in registry.section_rules(cfg.ticker).items()}
    body_items = set(rules.fetch_body_items)

    for f in filings:
        items = {i.strip() for i in f.items.split(",") if i.strip()}
        if f.form in TEXT_FORMS and f.filed >= since_text:
            doc_id, data = _download(conn, sec, cik, f.accn, f.primary_doc, force=force, now=now)
            if data is None:
                continue
            stats["downloaded"] += 1
            text = html_to_text(data)
            sections = apply_rules(text, split_items(text, f.form), section_rules)
            if f.form == "10-K":
                audit = audit_report(text)
                if audit is not None:
                    sections.append(audit)
            _store_sections(conn, doc_id, sections)
            stats["sections"] += len(sections)
            found = {s.code for s in sections}
            missing = [c for c in REQUIRED_SECTIONS[f.form] if c not in found]
            if missing:
                stats["missing_sections"] += len(missing)
                log.warning("%s %s %s: sections not found: %s",
                            cfg.ticker, f.form, f.accn, missing)  # fmt: skip
        elif f.form == "8-K" and items & body_items:
            doc_id, data = _download(conn, sec, cik, f.accn, f.primary_doc, force=force, now=now)
            if data is not None:
                stats["downloaded"] += 1
                sections = split_8k_items(html_to_text(data))
                _store_sections(conn, doc_id, sections)
                stats["sections"] += len(sections)
        if f.form == "8-K" and "2.02" in items and f.filed >= since_text:
            index_page = sec.filing_document(cik, f.accn, f"{f.accn}-index.html").payload
            # Single-exhibit filings label the press release plain "EX-99".
            name = exhibit_by_type(index_page, "EX-99.1") or exhibit_by_type(index_page, "EX-99")
            if name is None:
                log.warning("%s 8-K %s: Item 2.02 without an EX-99.1 file", cfg.ticker, f.accn)
                continue
            insert_missing(
                conn,
                source_document,
                [_doc_row(company_id, cik, f, name, "EX-99.1")],
                key_cols=["accession_no", "doc_name"],
            )
            _, data = _download(conn, sec, cik, f.accn, name, force=force, now=now)
            if data is not None:
                stats["downloaded"] += 1
    if history_years:
        stats["history_10k"] = _mdna_history(
            conn, sec, cfg, company_id, cik, subs, today - timedelta(days=366 * history_years),
            since_text, section_rules, force=force, now=now,
        )  # fmt: skip
    return stats


def _mdna_history(
    conn: Connection,
    sec: SecClient,
    cfg: CompanyConfig,
    company_id: int,
    cik: str,
    subs: dict[str, Any],
    since: date,
    since_text: str,
    section_rules: dict[str, tuple[str, str]],
    *,
    force: bool,
    now: datetime,
) -> int:
    """MD&A of the 10-Ks filed between `since` and the normal text window."""
    older = [
        f for f in filings_since(sec, cik, subs, since, ("10-K",), force=force)
        if f.filed < since_text
        and (cfg.history_start is None or f.filed >= cfg.history_start.isoformat())
    ]  # fmt: skip
    insert_missing(
        conn,
        source_document,
        [_doc_row(company_id, cik, f, f.primary_doc, "PRIMARY") for f in older],
        key_cols=["accession_no", "doc_name"],
    )
    done = 0
    for f in older:
        doc_id, data = _download(conn, sec, cik, f.accn, f.primary_doc, force=force, now=now)
        if data is None:
            continue
        text = html_to_text(data)
        sections = apply_rules(text, split_items(text, f.form), section_rules)
        mdna = [s for s in sections if s.code in HISTORY_SECTIONS]
        if not mdna:
            log.warning("%s 10-K %s: MD&A not found", cfg.ticker, f.accn)
            continue
        _store_sections(conn, doc_id, mdna)
        done += 1
    return done


def run_documents_stage(
    companies: Sequence[CompanyConfig],
    sec: SecClient,
    registry: Registry,
    rules: SignalRules,
    engine: Engine,
    *,
    text_lookback_days: int = 400,
    event_lookback_days: int = 730,
    force: bool = False,
    history_years: dict[str, int] | None = None,  # industry group -> mdna_history_years
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                group = company_row(conn, cfg)["industry_group"]
                stats = process_company(
                    cfg, sec, registry, rules, conn,
                    today=today(), text_lookback_days=text_lookback_days,
                    event_lookback_days=event_lookback_days, force=force, now=now(),
                    history_years=(history_years or {}).get(group, 0),
                )  # fmt: skip
            log.info("%s: documents %s", cfg.ticker, stats)
            detail = ", ".join(f"{k}={v}" for k, v in stats.items())
            results.append(TaskResult(cfg.ticker, "documents", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: documents stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "documents", ok=False, detail=str(exc)))
    return results
