"""Resolve configured companies to CIKs and fetch their SEC submissions."""

from __future__ import annotations

import logging

from uw_pipeline.clients.sec import RawDocument, SecClient
from uw_pipeline.config import CompanyConfig

log = logging.getLogger(__name__)


class CompanyResolutionError(Exception):
    pass


def resolve_cik(cfg: CompanyConfig, ticker_map: dict[str, int]) -> int:
    """Pinned CIK wins. SEC's ticker map is only a fallback and a consistency check."""
    mapped = ticker_map.get(cfg.ticker)
    if cfg.cik is not None:
        if mapped is None:
            log.info("%s not in SEC ticker list; using pinned CIK %s", cfg.ticker, cfg.cik)
        elif mapped != cfg.cik:
            log.warning(
                "%s maps to CIK %s in SEC ticker list but config pins %s; using pinned CIK",
                cfg.ticker,
                mapped,
                cfg.cik,
            )
        return cfg.cik
    if mapped is None:
        raise CompanyResolutionError(f"ticker {cfg.ticker} not in SEC ticker list; pin a cik")
    return mapped


def fetch_submissions(
    cfg: CompanyConfig, sec: SecClient, ticker_map: dict[str, int], *, force: bool
) -> RawDocument:
    cik = resolve_cik(cfg, ticker_map)
    doc = sec.submissions(cik, force=force)
    sec_tickers = {t.upper() for t in doc.payload.get("tickers") or []}
    if cfg.ticker not in sec_tickers:
        log.warning(
            "%s (CIK %s): configured ticker not in SEC submissions tickers %s",
            cfg.ticker,
            cik,
            sorted(sec_tickers),
        )
    return doc
