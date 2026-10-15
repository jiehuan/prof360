"""SEC submissions JSON -> company master record."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from uw_pipeline.config import CompanyConfig, IndustryConfig


class CompanyRecord(BaseModel):
    cik: str
    ticker: str
    name: str
    legal_name: str
    sic_code: str | None
    sic_description: str | None
    industry_group: str
    is_financial: bool
    hq_city: str | None
    hq_state: str | None
    hq_country: str | None
    fiscal_year_end: str | None
    exchange: str | None
    state_of_incorporation: str | None
    is_active: bool
    source_url: str
    source_hash: str
    raw_path: str
    last_refreshed_at: datetime


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _city(v: Any) -> str | None:
    s = _clean(v)
    # EDGAR stores cities in upper case; title-case them for display.
    return s.title() if s and s.isupper() else s


def parse_sic(payload: dict[str, Any]) -> int | None:
    sic = _clean(payload.get("sic"))
    return int(sic) if sic and sic.isdigit() else None


def parse_submissions(
    payload: dict[str, Any],
    cfg: CompanyConfig,
    industry: IndustryConfig,
    *,
    source_url: str,
    source_hash: str,
    raw_path: str,
    refreshed_at: datetime,
) -> CompanyRecord:
    legal_name = _clean(payload.get("name"))
    if not legal_name:
        raise ValueError("submissions payload has no company name")
    sic = parse_sic(payload)
    group, _ = industry.classify(sic)

    business = (payload.get("addresses") or {}).get("business") or {}
    foreign = bool(business.get("isForeignLocation"))
    state_or_country = _clean(business.get("stateOrCountry"))

    tickers = [t.upper() for t in payload.get("tickers") or []]
    exchanges = payload.get("exchanges") or []
    exchange = None
    if cfg.ticker in tickers and len(exchanges) == len(tickers):
        exchange = _clean(exchanges[tickers.index(cfg.ticker)])

    fye = _clean(payload.get("fiscalYearEnd"))
    return CompanyRecord(
        cik=str(payload["cik"]).zfill(10),
        ticker=cfg.ticker,
        name=cfg.display_name or legal_name,
        legal_name=legal_name,
        sic_code=str(sic) if sic is not None else None,
        sic_description=_clean(payload.get("sicDescription")),
        industry_group=group.code,
        is_financial=group.is_financial,
        hq_city=_city(business.get("city")),
        hq_state=None if foreign else state_or_country,
        hq_country=_clean(business.get("stateOrCountryDescription")) if foreign else "US",
        fiscal_year_end=fye.zfill(4) if fye else None,
        exchange=exchange,
        state_of_incorporation=_clean(payload.get("stateOfIncorporation")),
        is_active=cfg.active,
        source_url=source_url,
        source_hash=source_hash,
        raw_path=raw_path,
        last_refreshed_at=refreshed_at,
    )
