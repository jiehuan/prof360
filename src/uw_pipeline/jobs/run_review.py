"""Human review of what the pipeline could not settle (`uw-pipeline review`, decisions D-045).

Items: signals the rule/LLM vote left DISPUTED or OVERRULED, stored values that break an
identity (the latest dq run's `metric_identity` errors), litigation or regulatory matters
first seen after a company's baseline (Phase 15e: a new matter, or a known one named
differently), and users' reports of a metric value that looks wrong (Phase 19.C, D-092: one
item per company, metric and period; approving one says the value needs a fix, and needs a
note saying which). Each item shows its evidence and source link. Only a named reviewer decides;
nothing is approved automatically. A rejected signal or matter is left out of the signals,
the insight input and the review queue; an approved one is kept and leaves the queue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select

from uw_pipeline.enrich.industry_choice import review_key as industry_review_key
from uw_pipeline.enrich.legal import review_key
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.enrich.sources import filing_index_url
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import (
    company,
    dq_finding,
    industry_choice,
    legal_matter,
    metric_chat_context,
    metric_issue_report,
    review_decision,
    signal,
    source_document,
)

REVIEW_STATUSES = ("DISPUTED", "OVERRULED")


class ReviewError(Exception):
    """A reviewer's command that cannot be applied; the message says what to do instead."""


@dataclass(frozen=True)
class Item:
    item_type: str  # SIGNAL | IDENTITY | MATTER | INDUSTRY (D-059) | REPORT (D-092)
    item_key: str
    company_id: int
    ticker: str
    title: str
    status: str
    evidence: str
    source: str  # link to the filing, or where to look
    # REPORT: the review_decision keys of the reports this item groups (report:<id>); a
    # decision is recorded for each.
    keys: tuple[str, ...] = ()

    @property
    def short(self) -> str:
        """What the reviewer types: stable, short, unique."""
        return f"{self.item_type[0]}-{hashlib.sha1(self.item_key.encode()).hexdigest()[:8]}"


def _decided(conn: Connection) -> set[tuple[str, str]]:
    rows = conn.execute(select(review_decision.c.item_type, review_decision.c.item_key)).all()
    return {(t, k) for t, k in rows}


def pending(conn: Connection, report_source: str | None = None) -> list[Item]:
    """Open items; `report_source` (USER | MODEL) keeps only reports with a report of that
    source, other item types unchanged."""
    decided = _decided(conn)
    items: list[Item] = []
    for r in conn.execute(
        select(signal, company.c.ticker, company.c.cik, source_document.c.url)
        .join(company, company.c.company_id == signal.c.company_id)
        .outerjoin(source_document, source_document.c.doc_id == signal.c.doc_id)
        .where(signal.c.review_status.in_(REVIEW_STATUSES))
        .order_by(company.c.ticker, signal.c.event_date)
    ).mappings().all():  # fmt: skip
        if ("SIGNAL", r["signal_key"]) in decided:
            continue
        link = r["url"] or (filing_index_url(r["cik"], r["accession_no"])
                            if r["accession_no"] else "(no filing)")  # fmt: skip
        items.append(Item("SIGNAL", r["signal_key"], r["company_id"], r["ticker"],
                          f"{r['event_date']} {r['title']}", r["review_status"],
                          (r["snippet"] or r["description"])[:400], link))  # fmt: skip
    latest = conn.execute(select(func.max(dq_finding.c.run_id))).scalar()
    if latest is not None:
        for r in conn.execute(
            select(dq_finding, company.c.ticker)
            .join(company, company.c.company_id == dq_finding.c.company_id)
            .where(dq_finding.c.run_id == latest, dq_finding.c.rule_id == "metric_identity",
                   dq_finding.c.severity == "Error")
        ).mappings().all():  # fmt: skip
            key = f"{r['company_id']}:{r['message']}"[:100]
            if ("IDENTITY", key) in decided:
                continue
            items.append(Item("IDENTITY", key, r["company_id"], r["ticker"], r["message"],
                              "BROKEN", r["message"], "dq-report of the latest run"))  # fmt: skip
    for r in conn.execute(
        select(legal_matter, company.c.ticker, company.c.cik, source_document.c.accession_no)
        .join(company, company.c.company_id == legal_matter.c.company_id)
        .join(source_document, source_document.c.doc_id == legal_matter.c.last_doc_id)
        .where(legal_matter.c.review_status == "NEW")
        .order_by(company.c.ticker, legal_matter.c.first_disclosed)
    ).mappings().all():  # fmt: skip
        key = review_key(r["company_id"], r["matter_key"])
        if ("MATTER", key) in decided:
            continue
        link = filing_index_url(r["cik"], r["accession_no"])
        items.append(Item("MATTER", key, r["company_id"], r["ticker"],
                          f"{r['first_disclosed']} {r['matter_type']}: {r['name']}", "NEW",
                          r["quote"][:400], link))  # fmt: skip
    for r in conn.execute(
        select(industry_choice, company.c.ticker, company.c.sic_code, company.c.sic_description)
        .join(company, company.c.company_id == industry_choice.c.company_id)
        .where(industry_choice.c.status == "DISPUTED")
        .order_by(company.c.ticker)
    ).mappings().all():  # fmt: skip
        key = industry_review_key(r["company_id"])
        if ("INDUSTRY", key) in decided:
            continue
        sic = f"SIC {r['sic_code']} {r['sic_description'] or ''}".strip()
        title = (f"industry {r['group_code']} (models: {r['extractor_group']} / "
                 f"{r['tiebreaker_group']}); {sic}")  # fmt: skip
        items.append(Item("INDUSTRY", key, r["company_id"], r["ticker"], title, "DISPUTED",
                          f"{r['reason']} \"{r['quote'][:300]}\"",
                          "10-K Item 1 (business)"))  # fmt: skip
    return items + report_items(conn, decided, report_source)


def report_key(report_id: int) -> str:
    return f"report:{report_id}"


def report_items(
    conn: Connection, decided: set[tuple[str, str]], source: str | None = None
) -> list[Item]:
    """Undecided user and chat reports, one item per company, metric and period (D-092): those
    with a user's report first. The evidence says who reported what, the value now, and what
    the chat context already flags (proxy or fallback tags, overrides, findings, peers)."""
    r, co = metric_issue_report.c, company.c
    groups: dict[tuple[int, str, str], list[Any]] = {}
    tickers: dict[int, str] = {}
    for row in conn.execute(
        select(metric_issue_report, co.ticker)
        .join(company, co.company_id == r.company_id)
        .order_by(r.report_id)
    ).mappings():  # fmt: skip
        if ("REPORT", report_key(row["report_id"])) in decided:
            continue
        groups.setdefault((row["company_id"], row["metric_code"], row["period"] or ""),
                          []).append(row)  # fmt: skip
        tickers[row["company_id"]] = row["ticker"]
    items = []
    for (cid, code, period), rows in groups.items():
        sources = {x["source"] for x in rows}
        if source and source.upper() not in sources:
            continue
        ctx = _context(conn, cid, code)
        name = (ctx.get("metric") or {}).get("name") or code
        when = period_text(period) if period else "period not given"
        n_user = sum(x["source"] == "USER" for x in rows)
        users = len({x["user_hash"] for x in rows})
        n_chat = len(rows) - n_user
        head = (f"{len(rows)} report{'s' * (len(rows) > 1)} from {users} "
                f"user{'s' * (users > 1)}: {n_user} by users, {n_chat} by the chat")  # fmt: skip
        said = [f"\"{(x['comment'] or x['issue_summary'] or '').strip()[:200]}\""
                for x in reversed(rows) if (x["comment"] or x["issue_summary"])][:2]  # fmt: skip
        asked = next((x["question"] for x in reversed(rows) if x["question"]), None)
        parts = [head, *said]
        if asked:
            parts.append(f'asked: "{asked[:150]}"')
        parts += _context_flags(ctx, period)
        items.append(Item(
            "REPORT", f"REPORT:{cid}:{code}:{period or '-'}:{rows[0]['report_id']}", cid,
            tickers[cid], f"{name} {when}", "USER" if n_user else "MODEL",
            " | ".join(parts)[:900], _filing(ctx, period),
            tuple(report_key(x["report_id"]) for x in rows),
        ))  # fmt: skip
    return sorted(items, key=lambda i: (i.status != "USER", i.ticker, i.title))


def period_text(period: str) -> str:
    return period if period.startswith("FY") else f"FY{period[:4]} Q{period[-1]}"


def _context(conn: Connection, company_id: int, code: str) -> dict[str, Any]:
    raw = conn.execute(
        select(metric_chat_context.c.context_json).where(
            metric_chat_context.c.company_id == company_id,
            metric_chat_context.c.metric_code == code)
    ).scalar_one_or_none()  # fmt: skip
    return json.loads(raw) if raw else {}


def _context_flags(ctx: dict[str, Any], period: str) -> list[str]:
    """What the chat context says about the reported period (or the latest one)."""
    if not ctx:
        return ["no chat context for this metric"]
    periods = ctx.get("periods") or []
    p = next((x for x in periods if x.get("period") == period), periods[0] if periods else {})
    points = {d["id"]: d for d in ctx.get("data_points") or []}
    unit = (ctx.get("metric") or {}).get("unit") or "USD"
    out = []
    if p:
        value = points.get(p.get("value_id"), {})
        shown = fmt_value(value.get("value"), value.get("unit") or unit, p.get("status", "OK"))
        out.append(f"now {shown} ({period_text(p['period'])}, {p.get('derivation')})")
        if p.get("nm_reason"):
            out.append(p["nm_reason"])
        tags = [*p.get("tags", []), *(t for i in p.get("inputs", []) for t in i.get("tags", []))]
        for t in tags:
            if t.get("proxy"):
                out.append(f"proxy tag {t['tag']}: {t['proxy']}")
            elif t.get("fallback"):
                out.append(f"fallback tag {t['tag']} ({t.get('candidate')})")
    for concept, note in (ctx.get("company_overrides") or {}).items():
        out.append(f"override {concept}: {note}")
    for f in ctx.get("dq_findings") or []:
        out.append(f"dq {f['severity']} {f['rule']}: {f['message']}")
    peer = ctx.get("peer_median")
    if peer:
        median = points.get(peer["id"], {}).get("value")
        out.append(f"group median {fmt_value(median, unit)} (FY{peer['fiscal_year']}, "
                   f"{peer['peers']} peers)")  # fmt: skip
    return out


def _filing(ctx: dict[str, Any], period: str) -> str:
    periods = ctx.get("periods") or []
    p = next((x for x in periods if x.get("period") == period), periods[0] if periods else {})
    accn = p.get("accession")
    if not accn and p.get("inputs"):
        accn = next((i.get("accession") for i in p["inputs"] if i.get("accession")), None)
    filing = next((f for f in ctx.get("filings") or [] if f["accession"] == accn), None)
    if filing:
        return str(filing.get("url") or filing.get("title"))
    return str((p.get("source") or {}).get("url") or "metric detail (chat context)")


def decide(
    conn: Connection, short: str, decision: str, reviewer: str, note: str | None, now: datetime
) -> Item:
    if decision not in ("APPROVED", "REJECTED"):
        raise ReviewError(f"unknown decision {decision!r}: use approve or reject")
    if not reviewer.strip():
        raise ReviewError("a named reviewer is required: --reviewer 'First Last'")
    if decision == "APPROVED" and short.strip().startswith("R-") and not (note or "").strip():
        raise ReviewError(
            "approving a report says the value needs a fix: add --note with the fix (company "
            "override, concept_map proxy, coverage note, decision D-xxx)"
        )
    matches = [i for i in pending(conn) if i.short == short.strip()]
    if not matches:
        raise ReviewError(
            f"no open item {short!r}. It may be decided already or no longer open; run "
            "`uw-pipeline review` to see the current list and copy the id from it."
        )
    item = matches[0]
    upsert(conn, review_decision, [{
        "item_type": item.item_type, "item_key": key, "company_id": item.company_id,
        "decision": decision, "reviewer": reviewer.strip()[:100],
        "note": (note or "")[:1000] or None, "decided_at": now,
    } for key in (item.keys or (item.item_key,))], key_cols=["item_type", "item_key"])  # fmt: skip
    return item
