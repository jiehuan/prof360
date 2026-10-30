"""Raw XBRL facts -> standardized quarterly and annual values per (tag, dims, unit).

Rules (PLAN §5.2):
- Restatements: for the same tag and fiscal period, the most recently filed value wins.
  A fourth quarter is not derived from a year and a nine-month figure on different bases (a
  filing between them restated a fiscal year): it is emitted as N/M instead (D-052).
- Duration facts are classified by length: quarter (~3m), YTD 6m, YTD 9m, or fiscal year.
- Quarterly flow values come from a reported 3-month fact. Otherwise they are derived from
  YTD differences (Q4 = FY - 9M, cash-flow quarters from cumulative YTD). Derived values are
  marked DERIVED. Only USD flows are derived; other units (share counts) are not additive and
  keep reported periods only.
- Reported 6m/9m YTD values are also emitted (period_type "YTD") so that quarters can be
  derived at concept level when a company switches tags between its 10-Qs and its 10-K.
- Balance-sheet (instant) facts map to the fiscal quarter whose end they fall on. The Q4
  instant is also the FY value.
- Fiscal year labels follow the company's own convention (e.g. Macy's year ending
  2026-01-31 is fiscal 2025), learned from the `fy` the company used in its 10-Ks.
"""

from __future__ import annotations

import calendar
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from uw_pipeline.frames import records

log = logging.getLogger(__name__)

PERIOD_COLUMNS = [
    "taxonomy", "tag", "dims", "unit", "period_type", "fiscal_year", "fiscal_quarter",
    "period_end", "value", "derivation", "accn", "filed",
]  # fmt: skip

# Period length classes in days (end - start). Index = number of fiscal quarters covered.
# 12/12/12/16-week years (Costco, PepsiCo): a 16-week Q4 is 112 days, 36 weeks 252.
_DURATION_CLASSES = {1: (75, 115), 2: (160, 195), 3: (245, 285), 4: (350, 380)}
# 52/53-week calendars put period ends a few days off the nominal month end.
_END_TOLERANCE_DAYS = 10
# A quarter end the company's own 10-Q reports matches within this many days (Phase 16).
_LEARNED_TOLERANCE_DAYS = 4


def _fye_on_or_after(d: date, mmdd: str) -> date:
    """First nominal fiscal-year end on or after `d` (within tolerance)."""
    month, day = int(mmdd[:2]), int(mmdd[2:])
    for year in (d.year - 1, d.year, d.year + 1):
        nominal = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        if d <= nominal + timedelta(days=_END_TOLERANCE_DAYS):
            return nominal
    raise AssertionError("unreachable")


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


@dataclass(frozen=True)
class FiscalCalendar:
    fye_mmdd: str  # fiscal year end as MMDD, from SEC submissions
    year_offset: int = 0  # fiscal_year label = calendar year of the FY end + offset
    # Quarter ends the company's own 10-Qs report, with their quarter (Phase 16): in a
    # 12/12/12/16-week year Q2 and Q3 end weeks before the nominal month end.
    quarter_ends: tuple[tuple[date, int], ...] = ()

    def locate(self, end: date) -> tuple[int, int] | None:
        """(fiscal_year, fiscal_quarter) for a period ending on `end`, or None if `end` is not
        near a fiscal quarter end."""
        fy_end = _fye_on_or_after(end, self.fye_mmdd)
        for q_end, quarter in self.quarter_ends:
            if abs((end - q_end).days) <= _LEARNED_TOLERANCE_DAYS:
                return fy_end.year + self.year_offset, quarter
        days_before = (fy_end - end).days
        quarter = 4 - round(days_before / 91.31)
        if not 1 <= quarter <= 4:
            return None
        nominal = _months_back(fy_end, 3 * (4 - quarter))
        if abs((end - nominal).days) > _END_TOLERANCE_DAYS:
            return None
        return fy_end.year + self.year_offset, quarter


def learn_calendar(facts: pd.DataFrame, fye_mmdd: str) -> FiscalCalendar:
    """Infer the fiscal-year label offset from the `fy` companies put on their own 10-K facts.
    When the latest 10-K year does not end on `fye_mmdd` (SEC's `fiscalYearEnd`, which can be
    wrong: Brown & Brown's says 0119 for December years), the 10-K's own year end wins."""
    annual = facts[
        facts["form"].astype(str).str.startswith("10-K")
        & (facts["fp"] == "FY")
        & facts["fy"].notna()
        & facts["start"].notna()
    ]
    years: list[tuple[date, int]] = []  # (end of the 10-K's own year, its `fy`)
    for _, group in annual.groupby("accn"):
        end = max(date.fromisoformat(e) for e in group["end"])
        start = date.fromisoformat(group.loc[group["end"] == end.isoformat(), "start"].iloc[0])
        if 350 <= (end - start).days <= 380:
            years.append((end, int(group["fy"].iloc[0])))
    base = FiscalCalendar(fye_mmdd)
    # Only the latest 10-K year counts: a company that moved its year end (H&R Block from April
    # to June) has older years on the old date, and SEC's current value is right for it.
    latest = max((end for end, _ in years), default=None)
    if latest is not None and (base.locate(latest) or (0, 0))[1] != 4:
        log.warning("fiscal year end %s does not match the latest 10-K year (ends %s); using %s",
                    fye_mmdd, latest, latest.strftime("%m%d"))  # fmt: skip
        fye_mmdd = latest.strftime("%m%d")
        base = FiscalCalendar(fye_mmdd)
    offsets: Counter[int] = Counter()
    for end, fy in years:
        loc = base.locate(end)
        if loc is not None and loc[1] == 4:
            offsets[fy - loc[0]] += 1
    offset = offsets.most_common(1)[0][0] if offsets else 0
    return FiscalCalendar(fye_mmdd, offset, _quarter_ends(facts))


def _quarter_ends(facts: pd.DataFrame) -> tuple[tuple[date, int], ...]:
    """Each 10-Q's own quarter end (the latest end of its duration facts) and its quarter
    (the filing's `fp`)."""
    quarterly = facts[
        facts["form"].astype(str).str.startswith("10-Q")
        & facts["fp"].isin(["Q1", "Q2", "Q3"])
        & facts["start"].notna()
    ]
    out = set()
    for _, group in quarterly.groupby("accn"):
        end = max(date.fromisoformat(e) for e in group["end"])
        out.add((end, int(str(group["fp"].iloc[0])[1])))
    return tuple(sorted(out))


def _classify(facts: pd.DataFrame, cal: FiscalCalendar) -> pd.DataFrame:
    """Add fiscal_year, fiscal_quarter and span (0 = instant, 1-4 = quarters covered)."""
    out: list[dict[str, Any]] = []
    for row in records(facts):
        end = date.fromisoformat(row["end"])
        loc = cal.locate(end)
        if loc is None:
            continue
        start = row["start"]
        if start is None or (isinstance(start, float) and pd.isna(start)):
            span = 0
        else:
            days = (end - date.fromisoformat(start)).days
            span = next((k for k, (lo, hi) in _DURATION_CLASSES.items() if lo <= days <= hi), -1)
            # A YTD fact must start at the fiscal-year start, i.e. cover exactly fq quarters.
            if span == -1 or (span > 1 and span != loc[1]):
                continue
        out.append(row | {"fiscal_year": loc[0], "fiscal_quarter": loc[1], "span": span})
    return pd.DataFrame(out)


def _latest(group: pd.DataFrame) -> dict[str, Any]:
    return records(group.sort_values(["filed", "accn"]).tail(1))[0]


RESTATED = 0.005  # relative difference that counts as a restatement


def standardize(
    facts: pd.DataFrame, cal: FiscalCalendar, restated: list[str] | None = None
) -> pd.DataFrame:
    """`restated`, when given, collects the values held at their year's basis (D-046)."""
    restated = [] if restated is None else restated
    if facts.empty:
        return pd.DataFrame(columns=PERIOD_COLUMNS)
    classified = _classify(facts, cal)
    if classified.empty:
        return pd.DataFrame(columns=PERIOD_COLUMNS)

    # Restatements: one value per (series, fiscal period, span), latest filing wins ...
    keys = ["taxonomy", "tag", "dims", "unit", "fiscal_year", "fiscal_quarter", "span"]
    groups: dict[tuple[Any, ...], pd.DataFrame] = {
        tuple(k): g for k, g in classified.groupby(keys, sort=False)
    }
    latest: dict[tuple[Any, ...], dict[str, Any]] = {k: _latest(g) for k, g in groups.items()}
    # Basis changes per series: filings that restated a fiscal year's value (D-052). A
    # restated year means everything filed before the restatement is on the older basis.
    changes: dict[tuple[Any, ...], list[str]] = {}
    for k, g in groups.items():
        if int(k[6]) != 4:
            continue
        ordered = records(g.sort_values(["filed", "accn"]))
        first = ordered[0]["val"]
        changes.setdefault(k[:4], []).extend(
            r["filed"] for r in ordered[1:]
            if abs(r["val"] - first) > RESTATED * max(abs(first), 1.0)
        )  # fmt: skip

    def mixed_basis(series: tuple[Any, ...], older: str, newer: str) -> bool:
        """A basis change lies between two filing dates."""
        return any(older < c <= newer for c in changes.get(series, ()))

    # ... except that a quarter or year-to-date value restated after its fiscal year was
    # reported keeps the basis of that year's figure until a filing restates the year too
    # (the next 10-K's comparative). Mixing bases made the quarters disagree with the year
    # (LUV capex, D-035); decisions D-046.
    year_filed = {k[:5]: row["filed"] for k, row in latest.items() if int(k[6]) == 4}
    for k, g in groups.items():
        cutoff = year_filed.get(k[:5])
        row = latest[k]
        if int(k[6]) in (0, 4) or cutoff is None or row["filed"] <= cutoff:
            continue
        earlier = g[g["filed"] <= cutoff]
        if earlier.empty:
            continue
        before = _latest(earlier)
        if abs(row["val"] - before["val"]) > RESTATED * max(abs(before["val"]), 1.0):
            # The year itself is already on the newer basis when a filing between the
            # quarter's report and the year's restated a fiscal year (BAC's FY2025 10-K recast
            # FY2024): the restated quarter matches its year and is kept (D-052).
            if mixed_basis(k[:4], before["filed"], cutoff):
                continue
            latest[k] = before
            restated.append(
                f"{k[1]} FY{k[4]} span {k[6]} q{k[5]}: {before['val']:,.0f} as "
                f"reported with the year, {row['val']:,.0f} restated {row['filed']}"
            )

    rows: list[dict[str, Any]] = []

    def emit(src: dict[str, Any], ptype: str, fq: int, value: float, how: str) -> None:
        rows.append(
            {
                "taxonomy": src["taxonomy"],
                "tag": src["tag"],
                "dims": src["dims"],
                "unit": src["unit"],
                "period_type": ptype,
                "fiscal_year": int(src["fiscal_year"]),
                "fiscal_quarter": fq,
                "period_end": src["end"],
                "value": value,
                "derivation": how,
                "accn": src["accn"],
                "filed": src["filed"],
            }
        )

    by_series: dict[tuple[Any, ...], dict[tuple[int, int, int], dict[str, Any]]] = {}
    for k, row in latest.items():
        by_series.setdefault(k[:4], {})[(int(k[4]), int(k[5]), int(k[6]))] = row

    for series, facts_by_period in by_series.items():
        is_instant = all(span == 0 for (_, _, span) in facts_by_period)
        years = sorted({fy for (fy, _, _) in facts_by_period})
        for fy in years:
            if is_instant:
                for fq in range(1, 5):
                    src = facts_by_period.get((fy, fq, 0))
                    if src is None:
                        continue
                    emit(src, "Q", fq, src["val"], "REPORTED")
                    if fq == 4:
                        emit(src, "FY", 0, src["val"], "REPORTED")
                continue

            direct = {fq: facts_by_period.get((fy, fq, 1)) for fq in range(1, 5)}
            if next(iter(facts_by_period.values()))["unit"] != "USD":
                # Not additive (a weighted-average share count, a per-share amount): quarters
                # and years only as reported; Q4 = FY - 9M would be meaningless (Phase 15c).
                for fq, reported in direct.items():
                    if reported is not None:
                        emit(reported, "Q", fq, reported["val"], "REPORTED")
                annual = facts_by_period.get((fy, 4, 4))
                if annual is not None:
                    emit(annual, "FY", 0, annual["val"], "REPORTED")
                continue

            def ytd(k: int, fy: int = fy, direct: Any = direct, fbp: Any = facts_by_period) -> Any:
                """(value, source row) for the k-quarter YTD, or None."""
                if k == 0:
                    return (0.0, None)
                src = fbp.get((fy, k, k)) if k > 1 else direct[1]
                if src is not None:
                    return (src["val"], src)
                parts = [direct[i] for i in range(1, k + 1)]
                if all(p is not None for p in parts):
                    return (sum(p["val"] for p in parts), parts[-1])
                return None

            for fq in range(1, 5):
                reported = direct[fq]
                if reported is not None:
                    emit(reported, "Q", fq, reported["val"], "REPORTED")
                    continue
                cur, prev = ytd(fq), ytd(fq - 1)
                if cur is not None and prev is not None:
                    if fq == 4 and mixed_basis(series, prev[1]["filed"], cur[1]["filed"]):
                        # Year and nine months on different bases: the difference would carry
                        # the whole restatement (BAC revenue, D-052). N/M, not a wrong value.
                        emit(cur[1], "Q", 4, float("nan"), "DERIVED")
                        continue
                    emit(cur[1], "Q", fq, cur[0] - prev[0], "DERIVED")

            annual = facts_by_period.get((fy, 4, 4))
            if annual is not None:
                emit(annual, "FY", 0, annual["val"], "REPORTED")
            # Reported 6m/9m YTD values, so concept-level derivation can bridge tag switches.
            for span in (2, 3):
                ytd_src = facts_by_period.get((fy, span, span))
                if ytd_src is not None:
                    emit(ytd_src, "YTD", span, ytd_src["val"], "REPORTED")

    result = pd.DataFrame(rows, columns=PERIOD_COLUMNS)
    return result.sort_values(
        ["taxonomy", "tag", "dims", "unit", "period_type", "fiscal_year", "fiscal_quarter"]
    ).reset_index(drop=True)
