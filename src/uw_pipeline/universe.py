"""Company universe (PLAN 17a, D-055): which listed SEC registrants are in scope, their industry
group and revenue, and the development sample (the largest companies of each group).

The result is a review list. Chosen companies are pinned by CIK in config/companies.yaml by hand;
nothing here runs inside a pipeline run.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from uw_pipeline.config import CompanyConfig, IndustryConfig

EXCHANGES = ("NYSE", "Nasdaq", "CBOE")
ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F", "N-CSR")
DOMESTIC_FORMS = ("10-K", "10-KT")
SPAC_SIC = 6770  # not in underwriting scope (D-055)
# Exchange-traded products (gold, oil, currency and crypto trusts) file 10-Ks under these codes;
# operating companies there (e.g. Uranium Royalty Corp.) stay in scope.
ETP_SICS = frozenset({6199, 6221})
ETP_NAME = re.compile(r"\b(trust|fund|etf)\b", re.IGNORECASE)
REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
INTEREST_INCOME_TAG = "InterestAndDividendIncomeOperating"  # banks without a revenue tag
FRAME_TAGS = (*REVENUE_TAGS, INTEREST_INCOME_TAG)
# A year's revenue more than this multiple of the year before is taken as a tagging error (a
# filer tagged $103B for $103M) and the earlier year is used.
JUMP = 20

IN_SCOPE = "in_scope"
SCOPE_REASONS = {
    IN_SCOPE: "10-K filer listed on NYSE, Nasdaq or Cboe",
    "foreign_issuer": "latest annual report is a 20-F or 40-F",
    "fund": "files N-CSR (closed-end fund)",
    "no_annual_report": "no annual report since the cut-off",
    "spac": "SPAC (SIC 6770), not in underwriting scope",
    "exchange_traded_product": "exchange-traded product (commodity, currency or crypto trust)",
}


def listed(tickers: Iterable[Mapping[str, Any]]) -> dict[int, tuple[str, str]]:
    """CIK -> (ticker, exchange) for registrants with a ticker on NYSE, Nasdaq or Cboe: the first
    such ticker in SEC's list, which puts common stock before preferred shares and warrants."""
    out: dict[int, tuple[str, str]] = {}
    for row in tickers:
        if row.get("exchange") in EXCHANGES and row.get("ticker"):
            out.setdefault(int(row["cik"]), (str(row["ticker"]).upper(), str(row["exchange"])))
    return out


def summarize(cik: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a submissions response the universe rule reads; landed instead of the
    response (D-055). `annual` is the latest filing date of each annual report form."""
    recent = payload.get("filings", {}).get("recent", {})
    annual: dict[str, str] = {}
    for form, day in zip(recent.get("form", []), recent.get("filingDate", []), strict=False):
        if form in ANNUAL_FORMS and day > annual.get(form, ""):
            annual[form] = day
    sic = str(payload.get("sic") or "").strip()
    return {
        "cik": cik,
        "name": payload.get("name") or "",
        "sic": int(sic) if sic.isdigit() else None,
        "sic_desc": payload.get("sicDescription") or "",
        "fye": payload.get("fiscalYearEnd"),
        "annual": annual,
    }


def annual_form(summary: Mapping[str, Any], since: date) -> str | None:
    """The form of the latest annual report filed on or after `since` (a 10-K on a tie)."""
    filed = [(day, form in DOMESTIC_FORMS, form) for form, day in summary["annual"].items()
             if day >= since.isoformat()]  # fmt: skip
    return max(filed)[2] if filed else None


def scope(summary: Mapping[str, Any], since: date) -> str:
    """IN_SCOPE or the reason the registrant is left out (SCOPE_REASONS)."""
    form = annual_form(summary, since)
    if form is None:
        return "no_annual_report"
    if form in ("20-F", "40-F"):
        return "foreign_issuer"
    if form == "N-CSR":
        return "fund"
    if summary["sic"] == SPAC_SIC:
        return "spac"
    if summary["sic"] in ETP_SICS and ETP_NAME.search(summary["name"]):
        return "exchange_traded_product"
    return IN_SCOPE


# -- config/universe.yaml -------------------------------------------------------------------


class Reclassified(BaseModel):
    cik: int
    ticker: str
    industry: str
    reason: str = Field(min_length=1)


class Skipped(BaseModel):
    cik: int
    ticker: str
    reason: str = Field(min_length=1)


class UniverseConfig(BaseModel):
    industry: list[Reclassified] = Field(default_factory=list)
    sample_skip: list[Skipped] = Field(default_factory=list)

    def check(self, industry: IndustryConfig) -> None:
        unknown = {r.ticker: r.industry for r in self.industry if r.industry not in industry.groups}
        if unknown:
            raise ValueError(f"universe.yaml: unknown industry group(s) {unknown}")
        ciks = [r.cik for r in self.industry]
        if len(set(ciks)) != len(ciks):
            raise ValueError("universe.yaml: a CIK is listed twice under industry")


def load_universe_config(config_dir: Path) -> UniverseConfig:
    path = config_dir / "universe.yaml"
    if not path.exists():
        return UniverseConfig()
    return UniverseConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@dataclass(frozen=True)
class Revenue:
    value: float
    period: str  # XBRL frame period, e.g. CY2025
    tag: str
    note: str = ""  # e.g. a newer value set aside as a tagging error

    @property
    def interest_income(self) -> bool:
        return self.tag == INTEREST_INCOME_TAG


def revenue_by_cik(
    frames: Mapping[tuple[str, str], Mapping[str, Any]], periods: Sequence[str]
) -> dict[int, Revenue]:
    """Each filer's revenue for ranking, from frames keyed (tag, period): the newest of
    `periods` (newest first) it reported, the largest of the revenue tags in that period;
    interest and dividend income only for a filer without any revenue tag. Tagging errors
    (a value 1,000 times too large) are set aside: a contract-revenue figure more than JUMP
    times the filer's total `Revenues`, and a year more than JUMP times the year before."""
    tagged: dict[tuple[int, str], dict[str, Revenue]] = defaultdict(dict)
    for (tag, period), payload in frames.items():
        if tag not in FRAME_TAGS or period not in periods:
            continue
        for row in payload.get("data", []):
            tagged[int(row["cik"]), period][tag] = Revenue(float(row["val"]), period, tag)
    per: dict[int, dict[str, Revenue]] = defaultdict(dict)
    for (cik, period), by_tag in tagged.items():
        best = max(by_tag.values(), key=lambda r: (not r.interest_income, r.value))
        total = by_tag.get("Revenues")
        if total and best is not total and total.value > 0 and best.value > JUMP * total.value:
            best = replace(total, note=f"{best.tag} {best.value / 1e9:,.1f}B set aside")
        per[cik][period] = best
    out = {}
    for cik, by_period in per.items():
        found = [by_period[p] for p in periods if p in by_period]  # newest first
        chosen = [r for r in found if not r.interest_income] or found
        r = chosen[0]
        if len(chosen) > 1 and chosen[1].value > 0 and r.value > JUMP * chosen[1].value:
            r = replace(chosen[1], note=f"{r.period} value {r.value / 1e9:,.1f}B set aside")
        out[cik] = r
    return out


@dataclass
class Entry:
    cik: int
    ticker: str
    exchange: str
    name: str
    sic: int | None
    sic_desc: str
    scope: str
    annual_form: str | None
    group: str = ""
    how: str = ""  # company (its `industry`), universe (universe.yaml), sic, range or default
    reason: str = ""  # universe.yaml's reason when `how` is universe
    revenue: Revenue | None = None
    configured: bool = False  # already in companies.yaml


def build(
    listing: Mapping[int, tuple[str, str]],
    summaries: Mapping[int, Mapping[str, Any]],
    revenue: Mapping[int, Revenue],
    industry: IndustryConfig,
    companies: Sequence[CompanyConfig],
    since: date,
    universe: UniverseConfig | None = None,
) -> list[Entry]:
    """One entry per listed registrant with a submissions summary; groups for those in scope:
    a configured company's `industry` (D-049), else universe.yaml's, else its SIC code."""
    by_cik = {c.cik: c for c in companies if c.cik is not None}
    reclassified = {r.cik: r for r in (universe.industry if universe else [])}
    out = []
    for cik, (ticker, exchange) in sorted(listing.items()):
        s = summaries.get(cik)
        if s is None:
            continue
        e = Entry(cik=cik, ticker=ticker, exchange=exchange, name=s["name"], sic=s["sic"],
                  sic_desc=s["sic_desc"], scope=scope(s, since), annual_form=annual_form(s, since),
                  revenue=revenue.get(cik), configured=cik in by_cik)  # fmt: skip
        if e.scope == IN_SCOPE:
            r = reclassified.get(cik)
            if r is not None and cik not in by_cik:
                e.group, e.how, e.reason = r.industry, "universe", r.reason
            else:
                group, e.how = industry.classify(e.sic, by_cik.get(cik))
                e.group = group.code
        out.append(e)
    return out


def sample(
    entries: Iterable[Entry], n: int, default_group: str, skip: Iterable[int] = ()
) -> dict[str, list[Entry]]:
    """The `n` in-scope companies with the largest revenue in each industry group (companies
    without revenue last); the default group and the `skip` CIKs are left out."""
    left_out = set(skip)
    by_group: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.scope == IN_SCOPE and e.group != default_group and e.cik not in left_out:
            by_group[e.group].append(e)

    def largest_first(e: Entry) -> tuple[bool, float]:
        return (e.revenue is None, -(e.revenue.value if e.revenue else 0.0))

    return {g: sorted(rows, key=largest_first)[:n] for g, rows in sorted(by_group.items())}


_STATE = re.compile(r"\s*/[A-Z]{2,3}/?\s*$", re.IGNORECASE)  # "/DE/", "/md/"
_SUFFIX = re.compile(
    r"[,\s]+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|n\.v|s\.a|l\.p|llc)"
    r"\.?$",
    re.IGNORECASE,
)
_KEEP_UPPER = {"&", "AG", "AMC", "II", "III", "LLC", "LP", "NV", "SA", "USA", "US"}


def display_name(sec_name: str) -> str:
    """A proposal for `display_name` from SEC's conformed name, for review: without a state
    suffix or a legal form, and in title case when SEC has it in capitals."""
    name = _STATE.sub("", sec_name).strip()
    for _ in range(2):  # "Holdings Co Ltd" style names lose one legal form at a time
        name = _SUFFIX.sub("", name).strip(" ,")
    if name.isupper():
        name = " ".join(w if w in _KEEP_UPPER else w.capitalize() for w in name.split())
    return name
