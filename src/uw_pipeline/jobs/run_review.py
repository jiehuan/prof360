"""Human review of what the pipeline could not settle (`uw-pipeline review`, decisions D-045).

Items: signals the rule/LLM vote left DISPUTED or OVERRULED, and stored values that break an
identity (the latest dq run's `metric_identity` errors). Each item shows its evidence and source
link. Only a named reviewer decides; nothing is approved automatically. A rejected signal is
left out of the insight input and the review queue; an approved one is kept and leaves the queue.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, func, select

from uw_pipeline.enrich.sources import filing_index_url
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import company, dq_finding, review_decision, signal, source_document

REVIEW_STATUSES = ("DISPUTED", "OVERRULED")


class ReviewError(Exception):
    """A reviewer's command that cannot be applied; the message says what to do instead."""


@dataclass(frozen=True)
class Item:
    item_type: str  # SIGNAL | IDENTITY
    item_key: str
    company_id: int
    ticker: str
    title: str
    status: str
    evidence: str
    source: str  # link to the filing, or where to look

    @property
    def short(self) -> str:
        """What the reviewer types: stable, short, unique."""
        return f"{self.item_type[0]}-{hashlib.sha1(self.item_key.encode()).hexdigest()[:8]}"


def _decided(conn: Connection) -> set[tuple[str, str]]:
    rows = conn.execute(select(review_decision.c.item_type, review_decision.c.item_key)).all()
    return {(t, k) for t, k in rows}


def pending(conn: Connection) -> list[Item]:
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
    return items


def decide(
    conn: Connection, short: str, decision: str, reviewer: str, note: str | None, now: datetime
) -> Item:
    if decision not in ("APPROVED", "REJECTED"):
        raise ReviewError(f"unknown decision {decision!r}: use approve or reject")
    if not reviewer.strip():
        raise ReviewError("a named reviewer is required: --reviewer 'First Last'")
    matches = [i for i in pending(conn) if i.short == short.strip()]
    if not matches:
        raise ReviewError(
            f"no open item {short!r}. It may be decided already or no longer open; run "
            "`uw-pipeline review` to see the current list and copy the id from it."
        )
    item = matches[0]
    upsert(conn, review_decision, [{
        "item_type": item.item_type, "item_key": item.item_key, "company_id": item.company_id,
        "decision": decision, "reviewer": reviewer.strip()[:100],
        "note": (note or "")[:1000] or None, "decided_at": now,
    }], key_cols=["item_type", "item_key"])  # fmt: skip
    return item
