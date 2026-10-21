"""Typed loaders for the YAML files under config/."""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from uw_pipeline.yaml_load import safe_load


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return safe_load(f)


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
    # Industry group regardless of SIC (D-049), e.g. when SEC's SIC code is too coarse; the
    # reason is required and shown by `uw-pipeline config show`.
    industry: str | None = None
    industry_reason: str | None = None
    # Business-model traits of this company on top of its group's (D-058), e.g. clinical_stage:
    # their metric sets are added and `trait_exclude_metrics` dropped.
    traits: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _industry_reason(self) -> CompanyConfig:
        if self.industry and not (self.industry_reason or "").strip():
            raise ValueError(f"{self.ticker}: `industry` needs an `industry_reason`")
        return self

    def registrant_ciks(self, cik: str) -> list[str]:
        """The company's CIK (10 digits) and its predecessor registrants' (Phase 16), whose
        filings hold its earlier history. A successor-only company (`history_start`, D-012)
        reads its own filings only."""
        if self.history_start is not None:
            return [cik]
        return [cik, *(str(c).zfill(10) for c in self.predecessor_ciks if str(c).zfill(10) != cik)]

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
    # Business-model traits (D-043, e.g. lodging, bank); each adds the metric sets that
    # `trait_metric_sets` lists for it, so a new group names its traits instead of its sets.
    traits: list[str] = Field(default_factory=list)
    # Years of older 10-Ks whose MD&A is kept for text-metric history (Phase 8, e.g. 5 years of
    # NIM / CET1 / NPL for banks). 0 = only the documents in the normal text window.
    mdna_history_years: int = 0
    # What this industry does not track (D-049): metrics from its sets (still computed, hidden,
    # when another tracked metric needs them) and signals by rule id (`8k_5.02` covers its CEO /
    # CFO variants); `signal_params` sets rule fields for this industry only.
    exclude_metrics: list[str] = Field(default_factory=list)
    exclude_signals: list[str] = Field(default_factory=list)
    signal_params: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SicRange(BaseModel):
    from_sic: int = Field(alias="from")
    to_sic: int = Field(alias="to")
    group: str


class IndustryConfig(BaseModel):
    default_group: str
    groups: dict[str, IndustryGroup]
    sic: dict[int, str] = Field(default_factory=dict)
    sic_ranges: list[SicRange] = Field(default_factory=list)
    trait_metric_sets: dict[str, list[str]] = Field(default_factory=dict)
    # Metrics a trait makes meaningless, dropped for companies with it (D-058).
    trait_exclude_metrics: dict[str, list[str]] = Field(default_factory=dict)
    common_metric_sets: list[str] = Field(default_factory=list)  # every group (governance)

    @model_validator(mode="after")
    def _check_refs(self) -> IndustryConfig:
        for code, group in self.groups.items():
            group.code = code
            unknown_traits = set(group.traits) - self.trait_metric_sets.keys()
            if unknown_traits:
                raise ValueError(f"group {code}: unknown traits {sorted(unknown_traits)}")
            extra = [*self.common_metric_sets,
                     *(s for t in group.traits for s in self.trait_metric_sets[t])]  # fmt: skip
            group.metric_sets = list(dict.fromkeys([*group.metric_sets, *extra]))
        referenced = {self.default_group, *self.sic.values(), *(r.group for r in self.sic_ranges)}
        unknown = referenced - self.groups.keys()
        if unknown:
            raise ValueError(f"unknown industry group(s) referenced: {sorted(unknown)}")
        # Ranges must not overlap, so a company's group never depends on their order (D-055).
        ranges = sorted(self.sic_ranges, key=lambda r: r.from_sic)
        for a, b in itertools.pairwise(ranges):
            if b.from_sic <= a.to_sic:
                raise ValueError(f"sic_ranges overlap: {a.from_sic}-{a.to_sic} {a.group} and "
                                 f"{b.from_sic}-{b.to_sic} {b.group}")  # fmt: skip
        return self

    def check_companies(self, companies: list[CompanyConfig]) -> None:
        unknown = {c.ticker: c.industry for c in companies
                   if c.industry and c.industry not in self.groups}  # fmt: skip
        if unknown:
            raise ValueError(f"companies.yaml: unknown industry group(s) {unknown}")
        traits = {c.ticker: t for c in companies for t in c.traits
                  if t not in self.trait_metric_sets}  # fmt: skip
        if traits:
            raise ValueError(f"companies.yaml: unknown trait(s) {traits}")

    def group_of(self, code: str, company: CompanyConfig) -> IndustryGroup:
        """The company's group by its stored code (company.industry_group), with the company's
        own traits (D-058)."""
        return self.with_traits(self.groups[code], company.traits)

    def with_traits(self, group: IndustryGroup, traits: list[str]) -> IndustryGroup:
        """The group as a company with extra `traits` sees it (D-058): their metric sets added,
        their excluded metrics dropped."""
        extra = [t for t in traits if t not in group.traits]
        if not extra:
            return group
        sets = [s for t in extra for s in self.trait_metric_sets[t]]
        drop = [m for t in extra for m in self.trait_exclude_metrics.get(t, [])]
        return group.model_copy(update={
            "traits": [*group.traits, *extra],
            "metric_sets": list(dict.fromkeys([*group.metric_sets, *sets])),
            "exclude_metrics": list(dict.fromkeys([*group.exclude_metrics, *drop])),
        })  # fmt: skip

    def classify(
        self, sic: int | None, company: CompanyConfig | None = None
    ) -> tuple[IndustryGroup, str]:
        """Return (group, how) where how is 'company' (the company's `industry` in
        companies.yaml, D-049), 'sic', 'range' or 'default'."""
        group, how = self._group(sic, company)
        return (self.with_traits(group, company.traits) if company else group), how

    def _group(self, sic: int | None, company: CompanyConfig | None) -> tuple[IndustryGroup, str]:
        if company is not None and company.industry:
            return self.groups[company.industry], "company"
        if sic is not None:
            if sic in self.sic:
                return self.groups[self.sic[sic]], "sic"
            for r in self.sic_ranges:
                if r.from_sic <= sic <= r.to_sic:
                    return self.groups[r.group], "range"
        return self.groups[self.default_group], "default"


def load_industry_config(config_dir: Path) -> IndustryConfig:
    return IndustryConfig.model_validate(_load_yaml(config_dir / "industry_groups.yaml"))
