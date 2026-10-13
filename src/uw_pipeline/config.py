"""Typed loaders for the YAML files under config/."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# -- companies.yaml ----------------------------------------------------------------------------


class CompanyConfig(BaseModel):
    ticker: str
    # Pinned CIK. Tickers get reassigned (e.g. PARA), so the CIK is the authoritative identity.
    cik: int | None = None
    display_name: str | None = None
    active: bool = True
    former_tickers: list[str] = Field(default_factory=list)
    # Earlier registrants whose filings hold this company's history (used from Phase 2).
    predecessor_ciks: list[int] = Field(default_factory=list)
    # Ignore facts for periods starting before this date (e.g. predecessor figures that a
    # successor registrant reports in its own filings).
    history_start: date | None = None
    # Names an article must mention (headline or summary) to count as news about the company
    # (Phase 9). Default: the first word of display_name.
    news_aliases: list[str] = Field(default_factory=list)
    # Symbols Alpha Vantage files this company under, newest first (default: the ticker). After a
    # ticker change AV keeps older news and transcripts under the old symbol.
    av_symbols: list[str] = Field(default_factory=list)
    notes: str | None = None

    def symbols(self) -> list[str]:
        return self.av_symbols or [self.ticker]

    def aliases(self) -> list[str]:
        if self.news_aliases:
            return self.news_aliases
        return [self.display_name.split()[0]] if self.display_name else [self.ticker]

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class CompaniesFile(BaseModel):
    companies: list[CompanyConfig]

    @model_validator(mode="after")
    def _unique(self) -> CompaniesFile:
        tickers = [c.ticker for c in self.companies]
        ciks = [c.cik for c in self.companies if c.cik is not None]
        if len(set(tickers)) != len(tickers):
            raise ValueError("duplicate ticker in companies.yaml")
        if len(set(ciks)) != len(ciks):
            raise ValueError("duplicate cik in companies.yaml")
        return self


def load_companies(config_dir: Path) -> list[CompanyConfig]:
    return CompaniesFile.model_validate(_load_yaml(config_dir / "companies.yaml")).companies


# -- industry_groups.yaml ----------------------------------------------------------------------


class IndustryGroup(BaseModel):
    code: str = ""
    label: str
    # Drives which front-end modules get data (FI-only: NIM, CET1, loan portfolio, ...).
    is_financial: bool
    # Curated metric list for this group: named sets from metrics.yaml plus extra codes.
    metric_sets: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    # Years of older 10-Ks whose MD&A is kept for text-metric history (Phase 8, e.g. 5 years of
    # NIM / CET1 / NPL for banks). 0 = only the documents in the normal text window.
    mdna_history_years: int = 0


class SicRange(BaseModel):
    from_sic: int = Field(alias="from")
    to_sic: int = Field(alias="to")
    group: str


class IndustryConfig(BaseModel):
    default_group: str
    groups: dict[str, IndustryGroup]
    sic: dict[int, str] = Field(default_factory=dict)
    sic_ranges: list[SicRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_refs(self) -> IndustryConfig:
        for code, group in self.groups.items():
            group.code = code
        referenced = {self.default_group, *self.sic.values(), *(r.group for r in self.sic_ranges)}
        unknown = referenced - self.groups.keys()
        if unknown:
            raise ValueError(f"unknown industry group(s) referenced: {sorted(unknown)}")
        return self

    def classify(self, sic: int | None) -> tuple[IndustryGroup, str]:
        """Return (group, how) where how is 'sic', 'range' or 'default'."""
        if sic is not None:
            if sic in self.sic:
                return self.groups[self.sic[sic]], "sic"
            for r in self.sic_ranges:
                if r.from_sic <= sic <= r.to_sic:
                    return self.groups[r.group], "range"
        return self.groups[self.default_group], "default"


def load_industry_config(config_dir: Path) -> IndustryConfig:
    return IndustryConfig.model_validate(_load_yaml(config_dir / "industry_groups.yaml"))
