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
import json
import logging
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, select, update

from uw_pipeline.clients.sec import SecClient, archive_folder_url
from uw_pipeline.compute.insiders import insider_metrics
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig
from uw_pipeline.extract.filings import (
    Filing,
    exhibit_by_type,
    filings_since,
    ownership_parties,
)
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row, fiscal_periods
from uw_pipeline.load.upsert import delete_keys, insert_missing, upsert
from uw_pipeline.normalize.form4 import parse_form4
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.sections import (
    Section,
    apply_rules,
    audit_report,
    chunk,
    split_8k_items,
    split_items,
)
from uw_pipeline.schema import (
    filing_section,
    insider_transaction,
    metric_value,
    section_chunk,
    signal,
    source_document,
)
from uw_pipeline.signals.rules import SignalRules

log = logging.getLogger(__name__)

TEXT_FORMS = ("10-K", "10-Q")
EVENT_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A")
PROXY_FORMS = ("DEF 14A",)
# Sections that must be found; a miss is logged (PLAN Phase 3 acceptance).
HISTORY_SECTIONS = ("10K-7",)
REQUIRED_SECTIONS = {"10-K": ("10K-1A", "10K-3", "10K-7"), "10-Q": ("10Q-I-2",)}
# A 10-K MD&A shorter than this is a cross-reference to the annual report (D-060).
STUB_CHARS = 3_000
WRAPPER_ROLE = "10-K-WRAPPER"  # a 10-K whose text is in its Exhibit 13 annual report


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


def _filing_sections(
    conn: Connection, sec: SecClient, company_id: int, cik: str, f: Filing, doc_id: int,
    text: str, section_rules: dict[str, tuple[str, str]], *, now: datetime,
) -> tuple[int, list[Section]]:  # fmt: skip
    """A 10-K's or 10-Q's sections and the document they belong to. A 10-K whose MD&A only
    refers to the annual report filed as Exhibit 13 (D-060): the annual report becomes the
    filing's PRIMARY document, with the 10-K's own sections it lacks (Item 1, Part III) after
    its text; the 10-K is kept as a WRAPPER_ROLE document without sections."""
    sections = _split(text, f.form, section_rules)
    if f.form != "10-K":
        return doc_id, sections
    mdna = next((s for s in sections if s.code == "10K-7"), None)
    if mdna is not None and len(mdna.text) >= STUB_CHARS:
        return doc_id, sections
    index_page = sec.filing_document(cik, f.accn, f"{f.accn}-index.html").payload
    name = exhibit_by_type(index_page, "EX-13", numbered=True)
    if name is None:
        return doc_id, sections
    insert_missing(conn, source_document, [_doc_row(company_id, cik, f, name, "PRIMARY")],
                   key_cols=["accession_no", "doc_name"])  # fmt: skip
    conn.execute(update(source_document).where(source_document.c.doc_id == doc_id)
                 .values(doc_role=WRAPPER_ROLE))  # fmt: skip
    _store_sections(conn, doc_id, [])
    report_id, data = _download(conn, sec, cik, f.accn, name, force=True, now=now)
    report = html_to_text(data or b"")
    found = _split(report, f.form, section_rules)
    codes = {s.code for s in found}
    shift = len(report) + 1
    kept = [Section(s.code, s.start + shift, s.end + shift, s.text)
            for s in sections if s.code not in codes]  # fmt: skip
    log.info("%s %s: text from the annual report %s (Exhibit 13)", f.form, f.accn, name)
    return report_id, found + kept


def _split(text: str, form: str, section_rules: dict[str, tuple[str, str]]) -> list[Section]:
    sections = apply_rules(text, split_items(text, form), section_rules, form)
    if form == "10-K":
        audit = audit_report(text)
        if audit is not None:
            sections.append(audit)
    return sections


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
    stats: Counter[str] = Counter()
    kept_ownership: set[str] = set()
    # The company's own filings, then its predecessor registrants' (Phase 16: after a
    # holding-company reorganization the latest 10-K and proxy are the predecessor's).
    for cik in cfg.registrant_ciks(row["cik"]):
        stats.update(_process_registrant(
            cfg, sec, registry, rules, conn, row["company_id"], cik, kept_ownership,
            today=today, text_lookback_days=text_lookback_days,
            event_lookback_days=event_lookback_days, force=force, now=now,
            history_years=history_years,
        ))  # fmt: skip
    _drop_stray_ownership(conn, row["company_id"], kept_ownership, rules)
    return dict(stats)


def _process_registrant(
    cfg: CompanyConfig, sec: SecClient, registry: Registry, rules: SignalRules,
    conn: Connection, company_id: int, cik: str, kept_ownership: set[str], *, today: date,
    text_lookback_days: int, event_lookback_days: int, force: bool, now: datetime,
    history_years: int,
) -> dict[str, int]:  # fmt: skip
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
            doc_id, sections = _filing_sections(
                conn, sec, company_id, cik, f, doc_id, html_to_text(data), section_rules, now=now
            )
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
    # The latest proxy statement, for board composition (governance stage, D-045). Registered
    # and downloaded whole; its text is read by keyword, not split into items.
    proxies = filings_since(sec, cik, subs, since_events, PROXY_FORMS, force=force)
    if proxies:
        p = proxies[-1]
        insert_missing(conn, source_document,
                       [_doc_row(company_id, cik, p, p.primary_doc, "PRIMARY")],
                       key_cols=["accession_no", "doc_name"])  # fmt: skip
        _, data = _download(conn, sec, cik, p.accn, p.primary_doc, force=force, now=now)
        stats["proxy"] = 1 if data is not None else 0
    # Schedule 13D others filed about this company (activist stakes, Phase 15d): registered
    # from the submissions list only; the signal needs the form and the date, not the text.
    listed = filings_since(sec, cik, subs, since_events, tuple(rules.ownership_forms),
                           force=force) if rules.ownership_forms else []  # fmt: skip
    ownership = [f for f in listed if _about_company(sec, cik, f.accn)]
    insert_missing(conn, source_document,
                   [_doc_row(company_id, cik, f, f.primary_doc, "PRIMARY") for f in ownership],
                   key_cols=["accession_no", "doc_name"])  # fmt: skip
    kept_ownership |= {f.accn for f in ownership}
    stats["ownership"] = len(ownership)
    # Form 4s as far back as the insider-cluster rule looks (Phase 15b), at least the event window.
    cluster = rules.insiders.insider_cluster_selling
    since_form4 = min(since_events, today - timedelta(days=cluster.lookback_days)) if cluster \
        else since_events  # fmt: skip
    stats["form4"], stats["insider_values"] = _insiders(
        conn, sec, cfg, company_id, cik, subs, since_form4, force=force, now=now)  # fmt: skip
    if history_years:
        stats["history_10k"] = _mdna_history(
            conn, sec, cfg, company_id, cik, subs, today - timedelta(days=366 * history_years),
            since_text, section_rules, force=force, now=now,
        )  # fmt: skip
    return stats


def _drop_stray_ownership(
    conn: Connection, company_id: int, kept: set[str], rules: SignalRules
) -> None:
    """Earlier registrations of 13Ds the company filed about others (no signal points to
    them); `kept`: the 13Ds about the company, across its registrants."""
    stray = [r.doc_id for r in conn.execute(
        select(source_document.c.doc_id, source_document.c.accession_no).where(
            source_document.c.company_id == company_id,
            source_document.c.form_type.in_(tuple(rules.ownership_forms)))
    ).all() if r.accession_no not in kept]  # fmt: skip
    if stray:
        used = {d for (d,) in conn.execute(
            select(signal.c.doc_id).where(signal.c.doc_id.in_(stray))).all()}  # fmt: skip
        conn.execute(delete(source_document).where(
            source_document.c.doc_id.in_([d for d in stray if d not in used])))  # fmt: skip


def _about_company(sec: SecClient, cik: str, accn: str) -> bool:
    """Whether a Schedule 13D in the company's listing is about the company (its index headers
    name it as the subject), not one the company filed about another issuer."""
    header = sec.filing_document(cik, accn, f"{accn}-index-headers.html").payload
    subject, _ = ownership_parties(header.decode("utf-8", errors="replace"))
    return subject == int(cik)


def _insiders(
    conn: Connection, sec: SecClient, cfg: CompanyConfig, company_id: int, cik: str,
    subs: dict[str, Any], since: date, *, force: bool, now: datetime,
) -> tuple[int, int]:  # fmt: skip
    """Insiders' Form 4s (Phase 15b): registered, downloaded once and parsed into
    insider_transaction, then the quarterly insider metrics (derivation FORM4, owned here).
    Amendments (4/A) are left out so a line is never counted twice; Form 4s about another
    issuer are registered (so they are not fetched again) but not parsed into rows."""
    filings = filings_since(sec, cik, subs, since, ("4",), force=force)
    for f in filings:
        name = f.primary_doc.rsplit("/", 1)[-1]  # the raw XML, not the XSL-rendered view
        insert_missing(conn, source_document, [_doc_row(company_id, cik, f, name, "PRIMARY")],
                       key_cols=["accession_no", "doc_name"])  # fmt: skip
        doc_id, data = _download(conn, sec, cik, f.accn, name, force=force, now=now)
        if data is None:
            continue  # unchanged: already parsed
        try:
            parsed = parse_form4(data)
        except ET.ParseError as exc:
            log.warning("%s Form 4 %s: not parseable XML (%s)", cfg.ticker, f.accn, exc)
            continue
        if parsed["issuer_cik"] != int(cik):
            # Filed by the company as an owner of another issuer (e.g. JPMorgan as a 10%
            # holder): not about this company's insiders.
            conn.execute(delete(insider_transaction).where(
                insider_transaction.c.accession_no == f.accn))  # fmt: skip
            continue
        owner = parsed["owner"]
        upsert(conn, insider_transaction, [{
            "accession_no": f.accn, "company_id": company_id, "doc_id": doc_id,
            "filed_at": f.filed, "owner_cik": owner["cik"], "owner_name": owner["name"],
            "is_director": owner["is_director"], "is_officer": owner["is_officer"],
            "officer_title": owner["officer_title"], "is_ten_percent": owner["is_ten_percent"],
            "updated_at": now, **t,
        } for t in parsed["transactions"]], key_cols=["accession_no", "line"])  # fmt: skip
    rows = [dict(r) for r in conn.execute(select(insider_transaction).where(
        insider_transaction.c.company_id == company_id)).mappings()]  # fmt: skip
    values = insider_metrics(rows, fiscal_periods(conn, company_id), since.isoformat())
    keys = ["company_id", "metric_code", "period_type", "fiscal_year", "fiscal_quarter"]
    units = {"insider_net_sales": "USD", "officer_sell_pct": "fraction"}
    out = [{
        "company_id": company_id, "metric_code": code, "period_type": k[0], "fiscal_year": k[1],
        "fiscal_quarter": k[2], "period_end": end, "value": value, "value_status": "OK",
        "unit": units[code], "derivation": "FORM4", "formula": f"{code} from Form 4 filings",
        "source_tags": json.dumps({"form4": ["SEC Form 4"]}), "source_accn": "",
        "computed_at": now,
    } for code, by_key in values.items() for k, (value, end) in by_key.items()]  # fmt: skip
    upsert(conn, metric_value, out, key_cols=keys)
    produced = {tuple(r[k] for k in keys[1:]) for r in out}
    existing = conn.execute(
        select(*(metric_value.c[k] for k in keys[1:])).where(
            metric_value.c.company_id == company_id, metric_value.c.derivation == "FORM4"
        )
    ).all()
    stale = [tuple(e) for e in existing if tuple(e) not in produced]
    if stale:
        delete_keys(conn, metric_value, keys[1:], stale,
                    metric_value.c.company_id == company_id)  # fmt: skip
    return len(filings), len(out)


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
        doc_id, sections = _filing_sections(
            conn, sec, company_id, cik, f, doc_id, html_to_text(data), section_rules, now=now
        )
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
