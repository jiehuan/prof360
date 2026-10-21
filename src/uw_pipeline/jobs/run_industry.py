"""`industry` stage: an industry group for companies whose SIC code maps to none (Phase 17g,
D-059).

Only companies in the default group without `industry` in companies.yaml. Both models read the
business section of the latest 10-K and choose one configured group; see
`enrich/industry_choice.py` for what is kept. The choice is stored in `industry_choice` and,
unless a reviewer rejected it, becomes the company's group; the company's metrics are then
computed again with that group's metrics (`recompute`).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, desc, select, update

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.industry_choice import (
    PROMPT_VERSION,
    catalog,
    checked,
    schema,
    usable_choice,
)
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, cached_call, short_transactions
from uw_pipeline.enrich.schemas import IndustryChoice
from uw_pipeline.enrich.untrusted import data_block
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import company, filing_section, industry_choice, source_document

log = logging.getLogger(__name__)

MAX_CHARS = 12_000


def _business_section(conn: Connection, company_id: int) -> tuple[Any, str] | None:
    """The latest 10-K and its Item 1 (or its first sections when Item 1 was not found)."""
    doc = conn.execute(
        select(source_document)
        .where(source_document.c.company_id == company_id,
               source_document.c.form_type == "10-K", source_document.c.doc_role == "PRIMARY")
        .order_by(desc(source_document.c.filed_at)).limit(1)
    ).mappings().first()  # fmt: skip
    if doc is None:
        return None
    rows = conn.execute(
        select(filing_section.c.section_code, filing_section.c.text)
        .where(filing_section.c.doc_id == doc["doc_id"])
        .order_by(filing_section.c.start_offset)
    ).all()  # fmt: skip
    item1 = [t for code, t in rows if code == "10K-1"]
    text = "\n".join(item1 or [t for _, t in rows[:3]])[:MAX_CHARS]
    return (doc, text) if text.strip() else None


def choose(
    conn: Connection, client: LLMClient, llm: LLMConfig, registry: Registry,
    industry: IndustryConfig, row: Any, now: datetime,
) -> str:  # fmt: skip
    """Store the models' choice for one company; returns what happened."""
    found = _business_section(conn, row["company_id"])
    if found is None:
        return "no 10-K business section yet"
    doc, text = found
    user = (f"Company: {row['legal_name']}\nSIC: {row['sic_code'] or 'none'} "
            f"{row['sic_description'] or ''}\n\n{data_block('business', text)}\n\n"
            f"Industry groups:\n{catalog(industry, registry)}")  # fmt: skip
    system = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    reads: list[tuple[str | None, IndustryChoice]] = []
    for spec in (llm.extractor, llm.tiebreaker):
        raw = cached_call(conn, client, spec, task="industry_choice", prompt_version=PROMPT_VERSION,
                          system=system, user=user, schema=schema(industry), now=now,
                          validate=IndustryChoice.model_validate)  # fmt: skip
        if raw is None:
            return "a model gave no usable answer"
        parsed = IndustryChoice.model_validate(raw)
        reads.append((checked(parsed, industry, text), parsed))
    (first, a), (second, b) = reads
    if first is None and second is None:
        return "no grounded choice"
    group, used = (second, b) if second is not None else (first, a)
    status = "AGREED" if first == second else "DISPUTED"
    upsert(conn, industry_choice, [{
        "company_id": row["company_id"], "group_code": group, "status": status,
        "extractor_group": first, "tiebreaker_group": second,
        "quote": used.quote.strip()[:1000], "reason": used.reason.strip()[:1000],
        "doc_id": doc["doc_id"], "model": f"{llm.extractor.model}+{llm.tiebreaker.model}",
        "prompt_version": PROMPT_VERSION,
        "input_hash": hashlib.sha256(f"{PROMPT_VERSION}\n{user}".encode()).hexdigest(),
        "updated_at": now,
    }], key_cols=["company_id"])  # fmt: skip
    return f"{group} ({status.lower()})"


def run_industry_stage(
    companies: Sequence[CompanyConfig],
    llm: tuple[LLMClient, LLMConfig] | None,
    registry: Registry,
    industry: IndustryConfig,
    engine: Engine,
    recompute: Callable[[list[CompanyConfig]], list[TaskResult]],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results, changed = [], []
    for cfg in companies:
        try:
            with short_transactions(engine) as conn:
                row = company_row(conn, cfg)
                sic = int(row["sic_code"]) if row["sic_code"] else None
                _, how = industry.classify(sic, cfg)
                if how != "default":
                    detail = f"in {row['industry_group']} ({how})"
                    results.append(TaskResult(cfg.ticker, "industry", ok=True, detail=detail))
                    continue
                if llm is None:
                    results.append(TaskResult(cfg.ticker, "industry", ok=False,
                                              detail="LLM not configured"))  # fmt: skip
                    continue
                detail = choose(conn, llm[0], llm[1], registry, industry, row, now())
                group = usable_choice(conn, row["company_id"]) or industry.default_group
                if group != row["industry_group"]:
                    financial = industry.groups[group].is_financial
                    conn.execute(update(company)
                                 .where(company.c.company_id == row["company_id"])
                                 .values(industry_group=group, is_financial=financial))  # fmt: skip
                    changed.append(cfg)
            log.info("%s: industry %s", cfg.ticker, detail)
            results.append(TaskResult(cfg.ticker, "industry", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: industry stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "industry", ok=False, detail=str(exc)))
    if changed:
        bad = [r for r in recompute(changed) if not r.ok]
        for r in bad:
            log.warning("%s: metrics not recomputed after the industry choice: %s", r.ticker,
                        r.detail)  # fmt: skip
    return results
