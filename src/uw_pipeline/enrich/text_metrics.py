"""Numbers from filing text (Phase 5a, decisions D-032): pure helpers, no I/O.

The model copies numbers exactly as written; this module converts them. A value is kept only if
its quote is found in the source and the number, as written, appears in the quote.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel

from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.enrich.grounding import is_grounded, normalize
from uw_pipeline.enrich.schemas import ExtractedValue, LoanSegment
from uw_pipeline.normalize.periods import FiscalCalendar

SCALES = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
# Values from two models (dual check) or two sources agree within this relative tolerance.
AGREE_REL = 0.005


class LoanPortfolioConfig(BaseModel):
    prompt_version: str
    groups: list[str]
    keywords: list[str]
    definition: str


class TextConfig(BaseModel):
    prompt_version: str
    # Effort for the extractor model on this task only (None = llm.yaml's).
    extractor_effort: str | None = None
    sections: list[str]
    earnings_release: bool
    max_chars: int
    preliminary: dict[str, str]
    loan_portfolio: LoanPortfolioConfig


def load_text_config(config_dir: Path) -> TextConfig:
    with (config_dir / "text_extraction.yaml").open(encoding="utf-8") as f:
        return TextConfig.model_validate(yaml.safe_load(f))


# -- choosing text -----------------------------------------------------------------------------


def select_chunks(
    chunks: Sequence[str],
    keyword_groups: Sequence[Sequence[str]],
    max_chars: int,
    *,
    keep_first: bool = False,
    per_group: int = 2,
) -> list[int]:
    """Indexes of chunks to send, in document order, within `max_chars`.

    Each keyword group (one per metric) first gets its `per_group` best chunks, so a metric
    mentioned rarely (e.g. "net yield on interest-earning assets") is not crowded out by a
    frequent one (e.g. "CET1"). Remaining room goes to the chunks with the most mentions.
    `keep_first` always includes chunk 0 (the top of a press release has the headline numbers).
    """
    lows = [c.lower() for c in chunks]

    def hits(i: int, words: Sequence[str]) -> int:
        return sum(lows[i].count(w.lower()) for w in words)

    order: list[int] = [0] if keep_first and chunks else []
    for words in keyword_groups:
        ranked = sorted(
            (i for i in range(len(chunks)) if hits(i, words)), key=lambda i: -hits(i, words)
        )
        order += ranked[:per_group]
    all_words = [w for g in keyword_groups for w in g]
    order += sorted(
        (i for i in range(len(chunks)) if hits(i, all_words)), key=lambda i: -hits(i, all_words)
    )
    chosen: list[int] = []
    total = 0
    for i in order:
        if i in chosen or total + len(chunks[i]) > max_chars:
            continue
        chosen.append(i)
        total += len(chunks[i])
    return sorted(chosen)


# -- numbers -----------------------------------------------------------------------------------

_NUM = re.compile(r"^\(?-?\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%?\)?$")


def parse_number(text: str) -> float | None:
    """'1,234.5' -> 1234.5; '(42)' -> -42.0; '$128.80' -> 128.8; '69.3%' -> 69.3."""
    t = text.strip().replace("\u2212", "-")
    m = _NUM.match(t)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    negative = (t.startswith("(") and t.endswith(")")) or t.startswith("-")
    return -value if negative else value


def number_in_quote(number_text: str, quote: str) -> bool:
    """The number as written appears in the quote (ignoring $, %, commas spacing)."""
    core = re.sub(r"[$%()\s]", "", number_text)
    if not core:
        return False
    q = normalize(quote).replace(" ", "")
    return core.lower() in q or core.replace(",", "").lower() in q.replace(",", "")


def to_metric_value(ext: ExtractedValue, metric: MetricDef) -> float | None:
    """Convert to the metric's stored unit (fractions for ratios; USD for money)."""
    raw = parse_number(ext.value)
    if raw is None:
        return None
    if metric.unit == "fraction":
        if ext.unit == "percent":
            return raw / 100
        if ext.unit == "basis_points":
            return raw / 10_000
        return None
    if metric.unit == "USD" and ext.unit == "usd":
        return raw * SCALES[ext.scale]
    return None


# -- periods -----------------------------------------------------------------------------------

PeriodKey = tuple[str, int, int]


# Period lengths in days (end - start) accepted for each kind; anything else (e.g. a merger stub
# period shown in a quarter column) is not stored.
_LENGTHS = {"quarter": (75, 105), "year": (350, 380)}


def period_keys(
    ext_period: str, period_end: str, cal: FiscalCalendar, period_start: str | None = None
) -> list[PeriodKey]:
    try:
        end = date.fromisoformat(period_end)
        start = date.fromisoformat(period_start) if period_start else None
    except ValueError:
        return []
    if ext_period in _LENGTHS:
        lo, hi = _LENGTHS[ext_period]
        if start is None or not lo <= (end - start).days <= hi:
            return []
    loc = cal.locate(end)
    if loc is None:
        return []
    fy, fq = loc
    if ext_period == "quarter":
        return [("Q", fy, fq)]
    if ext_period == "year":
        return [("FY", fy, 0)] if fq == 4 else []
    if ext_period == "point_in_time":
        return [("Q", fy, fq), *([("FY", fy, 0)] if fq == 4 else [])]
    return []  # year-to-date values are not stored


# -- candidates --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    metric_code: str
    key: PeriodKey
    period_end: str
    value: float
    evidence: str


def candidates(
    values: Iterable[ExtractedValue],
    metrics: Mapping[str, MetricDef],
    source: str,
    cal: FiscalCalendar,
) -> tuple[list[Candidate], int]:
    """Grounded, converted values; and how many were rejected (year-to-date values are skipped
    by design and not counted as rejected)."""
    out, rejected = [], 0
    for ext in values:
        if ext.period == "year_to_date":
            continue
        metric = metrics.get(ext.metric_code)
        value = to_metric_value(ext, metric) if metric else None
        keys = period_keys(ext.period, ext.period_end, cal, ext.period_start)
        ok = (
            metric is not None
            and value is not None
            and keys
            and is_grounded(ext.evidence, source)
            and number_in_quote(ext.value, ext.evidence)
        )
        if not ok:
            rejected += 1
            continue
        assert value is not None
        out += [Candidate(ext.metric_code, k, ext.period_end, value, ext.evidence) for k in keys]
    return out, rejected


def agree(a: float, b: float) -> bool:
    return abs(a - b) <= AGREE_REL * max(abs(a), abs(b), 1e-9)


def dual_checked(
    first: Sequence[Candidate], second: Sequence[Candidate]
) -> tuple[list[Candidate], int]:
    """Values both models found with the same metric, period and (nearly) the same number."""
    other = {(c.metric_code, c.key): c for c in second}
    kept, dropped = [], 0
    for c in first:
        match = other.get((c.metric_code, c.key))
        if match is not None and agree(c.value, match.value):
            kept.append(c)
        else:
            dropped += 1
    return kept, dropped


@dataclass(frozen=True)
class LoanRow:
    segment: str
    parent_segment: str
    period_end: str
    outstanding: float  # USD
    nonaccrual: float | None  # USD
    evidence: str


def loan_rows(segments: Iterable[LoanSegment], source: str) -> tuple[list[LoanRow], int]:
    """Grounded loan segments converted to USD, and how many were rejected."""
    rows, rejected = [], 0
    for s in segments:
        out = parse_number(s.outstanding)
        nonacc = parse_number(s.nonaccrual) if s.nonaccrual else None
        ok = (
            out is not None
            and is_grounded(s.evidence, source)
            and number_in_quote(s.outstanding, s.evidence)
            and (s.nonaccrual is None or number_in_quote(s.nonaccrual, s.evidence))
        )
        if not ok or out is None:
            rejected += 1
            continue
        scale = SCALES[s.scale]
        rows.append(
            LoanRow(
                segment=s.segment.strip(),
                parent_segment=s.parent_segment.strip(),
                period_end=s.period_end,
                outstanding=out * scale,
                nonaccrual=nonacc * scale if nonacc is not None else None,
                evidence=s.evidence,
            )
        )
    return rows, rejected


def loan_key(name: str) -> str:
    """Loan class names compared across models: case, footnote marks and punctuation ignored."""
    name = re.sub(r"\([a-z0-9]\)", "", name.lower())
    name = name.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", name))
