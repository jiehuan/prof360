"""Stage runners. Each company is processed and committed independently."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.sec import SecClient
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.enrich.industry_choice import usable_choice
from uw_pipeline.extract.company import fetch_submissions
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.company import parse_sic, parse_submissions
from uw_pipeline.schema import company as company_table

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskResult:
    ticker: str
    stage: str
    ok: bool
    detail: str = ""


def run_company_stage(
    companies: Sequence[CompanyConfig],
    sec: SecClient,
    industry: IndustryConfig,
    engine: Engine,
    *,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    ticker_map = sec.company_tickers(force=force)
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            doc = fetch_submissions(cfg, sec, ticker_map, force=force)
            sic = parse_sic(doc.payload)
            group, how = industry.classify(sic, cfg)
            if how == "company":
                log.info("%s: classified as %s by companies.yaml (SIC %s): %s", cfg.ticker,
                         group.code, sic, cfg.industry_reason)  # fmt: skip
            elif how == "default":  # SIC ranges are the normal case since Phase 17 (D-055)
                log.warning("%s: SIC %s is in no industry group; classified as %s", cfg.ticker,
                            sic, group.code)  # fmt: skip
            record = parse_submissions(
                doc.payload,
                cfg,
                industry,
                source_url=doc.url,
                source_hash=doc.content_hash,
                raw_path=doc.raw_path,
                refreshed_at=now(),
            )
            with engine.begin() as conn:
                if how == "default":  # the LLM's choice, when there is one (D-059)
                    chosen = _stored_choice(conn, record.cik)
                    if chosen and chosen in industry.groups:
                        record = record.model_copy(update={
                            "industry_group": chosen,
                            "is_financial": industry.groups[chosen].is_financial})  # fmt: skip
                upsert(conn, company_table, [record.model_dump()], key_cols=["cik"])
            log.info(
                "%s: CIK %s, SIC %s -> %s%s",
                cfg.ticker,
                record.cik,
                record.sic_code,
                record.industry_group,
                " (cached)" if doc.from_cache else "",
            )
            results.append(TaskResult(cfg.ticker, "company", ok=True, detail=record.industry_group))
        except Exception as exc:
            log.exception("%s: company stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "company", ok=False, detail=str(exc)))
    return results


def _stored_choice(conn: Connection, cik: str) -> str | None:
    """The usable LLM choice of group for this company, if it has a row and a choice."""
    company_id = conn.execute(
        select(company_table.c.company_id).where(company_table.c.cik == cik)
    ).scalar()
    return usable_choice(conn, company_id) if company_id is not None else None
