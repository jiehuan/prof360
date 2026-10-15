"""Phase 3 `signals` stage: rule-based signals from documents, sections and metrics."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.sec import SecClient
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.llm import LLMConfig, LLMUnavailableError
from uw_pipeline.extract.filings import instance_name
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import delete_keys, upsert
from uw_pipeline.normalize.xbrl_facts import text_facts_from_instance
from uw_pipeline.schema import (
    board_composition,
    filing_section,
    metric_value,
    signal,
    signal_vote,
    source_document,
)
from uw_pipeline.signals.detect import (
    Detected,
    Doc,
    auditor_info,
    auditor_signals,
    board_signals,
    eight_k_signals,
    keyword_signals,
    metric_signals,
    pay_signals,
)
from uw_pipeline.signals.rules import SignalRules
from uw_pipeline.signals.vote import Voter, VoteRow, merge

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
    cfg: CompanyConfig,
    sec: SecClient,
    rules: SignalRules,
    conn: Connection,
    voter: Voter | None = None,
) -> tuple[int, list[Detected], list[VoteRow]]:
    row = company_row(conn, cfg)
    company_id, cik = row["company_id"], row["cik"]
    docs = _docs(conn, company_id)
    sections = _sections(conn, company_id)

    # Names the company uses for itself, e.g. "Ford" in "Chief Financial Officer of Ford".
    own_names = [n.split()[0] for n in (row["name"], row["legal_name"]) if n]
    detected = eight_k_signals(docs, sections, rules, own_names)
    detected += keyword_signals(docs, sections, rules)

    votes: list[VoteRow] = []
    if voter is not None:
        voted: dict[int, list[Detected]] = {}
        for doc in docs:
            if doc.doc_role != "PRIMARY" or doc.doc_id not in sections:
                continue
            mine = [d for d in detected if d.doc_id == doc.doc_id]
            result = voter.vote_document(doc, row["legal_name"], sections[doc.doc_id], mine)
            if result is not None:
                voted[doc.doc_id], doc_votes = result
                votes += doc_votes
        detected = merge(detected, voted, rules)

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
            select(metric_value).where(metric_value.c.company_id == company_id)
        ).mappings()
    ]
    quarterly = [v for v in values if v["period_type"] == "Q"]
    by_accn = {d.accession_no: d.doc_id for d in docs if d.doc_role == "PRIMARY"}
    board = conn.execute(
        select(board_composition, source_document.c.filed_at, source_document.c.accession_no)
        .join(source_document, source_document.c.doc_id == board_composition.c.doc_id)
        .where(board_composition.c.company_id == company_id)
    ).mappings().first()  # fmt: skip
    governance = (board_signals(dict(board), board["filed_at"], board["accession_no"], rules)
                  if board else [])  # fmt: skip
    for s in [*metric_signals(quarterly, rules), *pay_signals(values, rules)]:
        detected.append(replace(s, doc_id=by_accn.get(s.accession_no or "")))
    detected += [replace(s, doc_id=board["doc_id"]) for s in governance] if board else []
    return company_id, detected, votes


def _store_votes(conn: Connection, company_id: int, votes: list[VoteRow], now: datetime) -> None:
    rows = [
        {
            "company_id": company_id,
            "doc_id": v.doc_id,
            "topic": v.topic,
            "rule_value": v.rule_value,
            "extractor_value": v.extractor_value,
            "tiebreak_value": v.tiebreak_value,
            "outcome": v.outcome,
            "status": v.status,
            "evidence": v.evidence[:2000] if v.evidence else None,
            "updated_at": now,
        }
        for v in votes
    ]
    upsert(conn, signal_vote, rows, key_cols=["doc_id", "topic"])


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
        "method": d.method,
        "review_status": d.review_status,
        "votes_json": d.votes_json,
        "detected_at": now,
    }


def run_signals_stage(
    companies: Sequence[CompanyConfig],
    sec: SecClient,
    rules: SignalRules,
    engine: Engine,
    *,
    llm: tuple[LLMClient, LLMConfig] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    """Rule signals, with rule + LLM voting on judgment signals when `llm` is given."""
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.begin() as conn:
                voter = Voter(llm[0], llm[1], rules, conn, now) if llm else None
                try:
                    company_id, detected, votes = detect_company(cfg, sec, rules, conn, voter)
                except LLMUnavailableError as exc:
                    log.error("LLM unavailable, continuing with rules only: %s", exc)
                    llm = None
                    company_id, detected, votes = detect_company(cfg, sec, rules, conn, None)
                stamp = now()
                _store_votes(conn, company_id, votes, stamp)
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
                if stale:  # chunked: SQL Server allows 2,100 parameters per statement
                    delete_keys(conn, signal, ["signal_key"], [(k,) for k in sorted(stale)],
                                signal.c.company_id == company_id)  # fmt: skip
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["severity"]] = counts.get(r["severity"], 0) + 1
            log.info(
                "%s: %d signals %s (%d stale removed)%s", cfg.ticker, len(rows), counts,
                len(stale), f" llm {voter.stats}" if voter else "",
            )  # fmt: skip
            results.append(
                TaskResult(cfg.ticker, "signals", ok=True, detail=f"{len(rows)} signals")
            )
        except Exception as exc:
            log.exception("%s: signals stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "signals", ok=False, detail=str(exc)))
    return results
