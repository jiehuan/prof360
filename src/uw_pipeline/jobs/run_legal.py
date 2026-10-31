"""`legal` stage: litigation and regulatory matters from the latest 10-K and the 10-Qs after it
(Phase 15e, D-050).

Both models read the legal passages of each filing (legal proceedings, the contingencies note,
risk updates); see `enrich/legal.py` for what is kept. Filings are read oldest first and each
matter is matched to the stored ones: a match updates its type, status, quote and last filing.
Everything found in a company's first run is its baseline; a matter first seen in a later run
(a new filing, or one named differently) is NEW and waits in `uw-pipeline review`.
Matters are never deleted: one no longer disclosed keeps its last filing date. The signals stage
turns recent securities, derivative and government matters into signals; the insight package
lists the current ones.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.clients.storage import Storage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.legal import Matter, agreed, checked, match, matter_key, tokens
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, cached_call, short_transactions
from uw_pipeline.enrich.schemas import LegalMatters, strict_json_schema
from uw_pipeline.enrich.text_metrics import select_chunks
from uw_pipeline.enrich.untrusted import data_block
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.html_text import html_to_text
from uw_pipeline.normalize.sections import chunk
from uw_pipeline.schema import legal_matter, source_document

log = logging.getLogger(__name__)

PROMPT_VERSION = "legal_matters_v1"
SCHEMA = strict_json_schema(LegalMatters)
MAX_CHARS = 45_000
KEYWORDS = [
    ["class action", "putative class", "securities litigation", "securities fraud"],
    ["derivative action", "derivative complaint", "derivative suit", "derivative lawsuit"],
    ["subpoena", "civil investigative demand", "wells notice", "investigation", "inquiry"],
    ["department of justice", "securities and exchange commission", "attorney general",
     "federal trade commission", "doj"],
    ["legal proceedings", "litigation", "lawsuit", "complaint"],
    ["contingencies", "reasonably possible", "accrued"],
]  # fmt: skip


class LLMUnavailable(Exception):
    """A model gave no usable answer: nothing is changed, the next run reads the filing again."""


@dataclass
class Stats:
    documents: int = 0
    matters: int = 0  # kept (both models, grounded) across filings
    unpaired: int = 0  # readings only one model gave, or with a different type
    new: int = 0  # first seen after the first filing: in the review queue
    stored: int = 0


def _filings(conn: Connection, company_id: int) -> list[Any]:
    """The latest 10-K and the 10-Qs filed after it, oldest first."""
    base = select(source_document).where(
        source_document.c.company_id == company_id, source_document.c.doc_role == "PRIMARY",
        source_document.c.raw_path.is_not(None),
    )  # fmt: skip
    ten_k = conn.execute(
        base.where(source_document.c.form_type == "10-K")
        .order_by(source_document.c.filed_at.desc())
        .limit(1)
    ).mappings().first()  # fmt: skip
    if ten_k is None:
        return []
    ten_qs = conn.execute(
        base.where(source_document.c.form_type == "10-Q",
                   source_document.c.filed_at > ten_k["filed_at"])
        .order_by(source_document.c.filed_at)
    ).mappings().all()  # fmt: skip
    return [ten_k, *ten_qs]


def read_filing(
    conn: Connection, client: LLMClient, llm: LLMConfig, storage: Storage, doc: Any,
    legal_name: str, now: datetime, ignore: frozenset[str] = frozenset(),
) -> tuple[list[Matter], int]:  # fmt: skip
    """Matters both models found in one filing, and the readings left unpaired."""
    chunks = [c.text for c in chunk(html_to_text(storage.read_bytes(doc["raw_path"])))]
    flat = [" ".join(c.split()) for c in chunks]  # table cells break lines
    idx = select_chunks(flat, KEYWORDS, MAX_CHARS, per_group=3, round_robin=True)
    if not idx:
        return [], 0
    source = "\n".join(chunks[i] for i in idx)
    user = (f"Company: {legal_name}\nDocument: {doc['form_type']} filed {doc['filed_at']}"
            f"\n\n{data_block('excerpt', source)}")  # fmt: skip
    system = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    reads = []
    for spec in (llm.extractor, llm.tiebreaker):
        raw = cached_call(conn, client, spec, task="legal_matters", prompt_version=PROMPT_VERSION,
                          system=system, user=user, schema=SCHEMA, now=now,
                          validate=LegalMatters.model_validate)  # fmt: skip
        if raw is None:
            raise LLMUnavailable(f"{doc['form_type']} filed {doc['filed_at']}")
        reads.append(checked(LegalMatters.model_validate(raw).matters, source))
    return agreed(reads[0], reads[1], ignore)


def merge(
    stored: dict[str, dict[str, Any]], matters: Sequence[Matter], doc: Any, *, baseline: bool,
    model: str, now: datetime, ignore: frozenset[str] = frozenset(),
) -> tuple[set[str], int]:  # fmt: skip
    """Fold one filing's matters into `stored` (matter_key -> row): (keys changed, new)."""
    changed, new = set(), 0
    for m in matters:
        hit = match(m, stored.values(), ignore)
        if hit is not None:
            row = stored[hit["matter_key"]]
            if doc["filed_at"] < row["last_seen"]:
                continue  # an older filing than the one already recorded
            row.update(
                matter_type=m.matter_type, status=m.status, quote=m.quote,
                last_doc_id=doc["doc_id"], last_seen=doc["filed_at"],
                wells_notice=row["wells_notice"] or m.wells_notice,
                accrual=m.accrual if m.accrual is not None else row["accrual"],
                agency=row["agency"] or m.agency, started=row["started"] or m.started,
                model=model, prompt_version=PROMPT_VERSION, updated_at=now,
            )  # fmt: skip
            changed.add(row["matter_key"])
            continue
        key = matter_key(m)
        new += not baseline
        stored[key] = {
            "company_id": doc["company_id"], "matter_key": key, "matter_type": m.matter_type,
            "name": m.name, "agency": m.agency, "status": m.status, "started": m.started,
            "wells_notice": m.wells_notice, "accrual": m.accrual, "quote": m.quote,
            "first_doc_id": doc["doc_id"], "last_doc_id": doc["doc_id"],
            "first_disclosed": doc["filed_at"], "last_seen": doc["filed_at"],
            "review_status": "BASELINE" if baseline else "NEW", "model": model,
            "prompt_version": PROMPT_VERSION, "updated_at": now,
        }  # fmt: skip
        changed.add(key)
    return changed, new


def process_company(
    cfg: CompanyConfig, client: LLMClient, llm: LLMConfig, storage: Storage, conn: Connection,
    now: datetime,
) -> Stats:  # fmt: skip
    row = company_row(conn, cfg)
    stats = Stats()
    filings = _filings(conn, row["company_id"])
    stored = {
        r["matter_key"]: dict(r)
        for r in conn.execute(
            select(legal_matter).where(legal_matter.c.company_id == row["company_id"])
        ).mappings()
    }
    # The company's first run sets its baseline; matters new in later runs go to review.
    baseline = not stored
    own = tokens(f"{row['name']} {row['legal_name']}")
    model = f"{llm.extractor.model}+{llm.tiebreaker.model}"
    changed: set[str] = set()
    for doc in filings:
        matters, unpaired = read_filing(conn, client, llm, storage, doc, row["legal_name"], now,
                                        own)  # fmt: skip
        stats.documents += 1
        stats.matters += len(matters)
        stats.unpaired += unpaired
        keys, new = merge(stored, matters, doc, baseline=baseline, model=model, now=now,
                          ignore=own)  # fmt: skip
        changed |= keys
        stats.new += new
    upsert(conn, legal_matter, [stored[k] for k in sorted(changed)],
           key_cols=["company_id", "matter_key"])  # fmt: skip
    stats.stored = len(stored)
    return stats


def run_legal_stage(
    companies: Sequence[CompanyConfig],
    llm: tuple[LLMClient, LLMConfig] | None,
    storage: Storage,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    if llm is None:
        return [TaskResult(c.ticker, "legal", ok=False, detail="LLM not configured")
                for c in companies]  # fmt: skip
    results = []
    for cfg in companies:
        try:
            with short_transactions(engine) as conn:
                stats = process_company(cfg, llm[0], llm[1], storage, conn, now())
            detail = str(vars(stats))
            log.info("%s: legal %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "legal", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: legal stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "legal", ok=False, detail=str(exc)))
    return results
