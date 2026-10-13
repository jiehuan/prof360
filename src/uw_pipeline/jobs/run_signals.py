"""Phase 3 `signals` stage: rule-based signals from documents, sections and metrics."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, select

from uw_pipeline.clients.sec import SecClient
from uw_pipeline.config import CompanyConfig
from uw_pipeline.extract.filings import instance_name
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.xbrl_facts import text_facts_from_instance
from uw_pipeline.schema import filing_section, metric_value, signal, source_document
from uw_pipeline.signals.detect import (
    Detected,
    Doc,
    auditor_info,
    auditor_signals,
    eight_k_signals,
    keyword_signals,
    metric_signals,
)
from uw_pipeline.signals.rules import SignalRules

log = logging.getLogger(__name__)


def _docs(conn: Connection, company_id: int) -> list[Doc]:
    rows = conn.execute(
        select(source_document).where(source_document.c.company_id == company_id)
    ).mappings()
    return [
        Doc(
            doc_id=r["doc_id"],
            form_type=r["form_type"],
            doc_role=r["doc_role"],
            accession_no=r["accession_no"],
            filed_at=r["filed_at"],
            period_end=r["period_end"],
            items=r["items"],
        )
        for r in rows
    ]


def _sections(conn: Connection, company_id: int) -> dict[int, dict[str, str]]:
    rows = conn.execute(
        select(filing_section.c.doc_id, filing_section.c.section_code, filing_section.c.text)
        .join(source_document, source_document.c.doc_id == filing_section.c.doc_id)
        .where(source_document.c.company_id == company_id)
    )
    out: dict[int, dict[str, str]] = {}
    for doc_id, code, text in rows:
        out.setdefault(doc_id, {})[code] = text
    return out


def _auditor_name(sec: SecClient, cik: str, accn: str, primary_doc: str) -> str | None:
    name = instance_name(sec.filing_index(cik, accn), primary_doc)
    if name is None:
        return None
    xml = sec.filing_document(cik, accn, name).payload
    return text_facts_from_instance(xml, ["dei:AuditorName"]).get("dei:AuditorName")


def detect_company(
    cfg: CompanyConfig, sec: SecClient, rules: SignalRules, conn: Connection
) -> tuple[int, list[Detected]]:
    row = company_row(conn, cfg)
    company_id, cik = row["company_id"], row["cik"]
    docs = _docs(conn, company_id)
    sections = _sections(conn, company_id)

    # Names the company uses for itself, e.g. "Ford" in "Chief Financial Officer of Ford".
    own_names = [n.split()[0] for n in (row["name"], row["legal_name"]) if n]
    detected = eight_k_signals(docs, sections, rules, own_names)
    detected += keyword_signals(docs, sections, rules)

    primary_names = dict(
        conn.execute(
            select(source_document.c.doc_id, source_document.c.doc_name).where(
                source_document.c.company_id == company_id
            )
        ).all()
    )
    ten_ks = [d for d in docs if d.form_type == "10-K" and d.doc_role == "PRIMARY"]
    infos = [
        auditor_info(
            d,
            _auditor_name(sec, cik, d.accession_no, primary_names[d.doc_id]),
            sections.get(d.doc_id, {}).get("10K-AUDIT"),
        )
        for d in ten_ks
    ]
    detected += auditor_signals(infos)

    values = [
        dict(r)
        for r in conn.execute(
            select(metric_value).where(
                and_(metric_value.c.company_id == company_id, metric_value.c.period_type == "Q")
            )
        ).mappings()
    ]
    by_accn = {d.accession_no: d.doc_id for d in docs if d.doc_role == "PRIMARY"}
    for s in metric_signals(values, rules):
        detected.append(Detected(**{**s.__dict__, "doc_id": by_accn.get(s.accession_no or "")}))
    return company_id, detected


def _row(company_id: int, d: Detected, now: datetime) -> dict[str, Any]:
    key_src = "|".join(
        [str(company_id), d.rule_id, d.accession_no or "", d.section_code or "", d.key_extra]
    )
    return {
        "signal_key": hashlib.sha1(key_src.encode()).hexdigest(),
        "company_id": company_id,
        "rule_id": d.rule_id,
        "category": d.category,
        "subcategory": d.subcategory,
        "severity": d.severity,
        "title": d.title[:300],
        "description": d.description[:2000],
        "event_date": d.event_date,
        "doc_id": d.doc_id,
        "accession_no": d.accession_no,
        "section_code": d.section_code,
        "snippet": d.snippet[:2000] if d.snippet else None,
        "detected_at": now,
    }


def run_signals_stage(
    companies: Sequence[CompanyConfig],
    sec: SecClient,
    rules: SignalRules,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                company_id, detected = detect_company(cfg, sec, rules, conn)
                stamp = now()
                rows = list(
                    {
                        r["signal_key"]: r for r in (_row(company_id, d, stamp) for d in detected)
                    }.values()
                )
                existing = {
                    k: t
                    for k, t in conn.execute(
                        select(signal.c.signal_key, signal.c.detected_at).where(
                            signal.c.company_id == company_id
                        )
                    ).all()
                }
                for r in rows:  # keep the first detection time on reruns
                    r["detected_at"] = existing.get(r["signal_key"], r["detected_at"])
                upsert(conn, signal, rows, key_cols=["signal_key"])
                stale = set(existing) - {r["signal_key"] for r in rows}
                if stale:
                    conn.execute(
                        delete(signal).where(
                            signal.c.company_id == company_id, signal.c.signal_key.in_(stale)
                        )
                    )
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["severity"]] = counts.get(r["severity"], 0) + 1
            log.info(
                "%s: %d signals %s (%d stale removed)", cfg.ticker, len(rows), counts, len(stale)
            )
            results.append(
                TaskResult(cfg.ticker, "signals", ok=True, detail=f"{len(rows)} signals")
            )
        except Exception as exc:
            log.exception("%s: signals stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "signals", ok=False, detail=str(exc)))
    return results
