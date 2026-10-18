"""`market` stage (Phase 15a, decisions D-050): daily prices and earnings estimates from Alpha
Vantage, and the market metrics at each fiscal period end.

Per company: TIME_SERIES_DAILY_ADJUSTED (and once per run the benchmark, SPY) into
`market_daily`, EARNINGS into `earnings_report`, then for every period the compute stage knows
(its period ends follow the company's fiscal calendar):
- market_cap: close on the last trading day on or before the period end times shares
  outstanding (the cover page of the first filing dated within 120 days from the period end, else
  diluted weighted-average shares for the period);
- volatility_90d: annualized standard deviation of the 63 daily log returns to the period end
  (adjusted closes, so splits are not returns);
- max_drawdown_52w: the largest fall from a running high in the year to the period end;
- eps_surprise_pct (quarters): (reported - estimate) / |estimate|.
Rows are stored with derivation MARKET; this stage owns them (removes ones it no longer
produces). Only metrics in the company's registry are written, so industry exclusions apply.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, select

from uw_pipeline.clients.alphavantage import AlphaVantageClient
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row, fiscal_periods
from uw_pipeline.load.upsert import delete_keys, upsert
from uw_pipeline.schema import earnings_report, market_daily, metric_value, xbrl_fact

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
KEEP_YEARS = 7  # price history kept: five fiscal years back plus the 52-week window
VOL_RETURNS = 63  # trading days in about 90 calendar days
MIN_RETURNS = 50
COVER_DAYS = 120  # a period's cover-page share count is dated within this after its end
PRICE_GAP_DAYS = 7  # the period-end close may be from up to a week before (holidays)
SOURCE = "Alpha Vantage TIME_SERIES_DAILY_ADJUSTED"
MARKET_CODES = {"market_cap", "volatility_90d", "max_drawdown_52w", "eps_surprise_pct"}


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def price_rows(payload: dict[str, Any], symbol: str, since: str, raw_path: str,
               now: datetime) -> list[dict[str, Any]]:  # fmt: skip
    rows = []
    for day, bar in (payload.get("Time Series (Daily)") or {}).items():
        close, adj = _num(bar.get("4. close")), _num(bar.get("5. adjusted close"))
        if day < since or close is None or adj is None:
            continue
        rows.append({"symbol": symbol, "trade_date": day, "close": close, "adjusted_close": adj,
                     "volume": _num(bar.get("6. volume")), "raw_path": raw_path,
                     "updated_at": now})  # fmt: skip
    return rows


def earnings_rows(payload: dict[str, Any], symbol: str, raw_path: str,
                  now: datetime) -> list[dict[str, Any]]:  # fmt: skip
    rows = []
    for q in payload.get("quarterlyEarnings") or []:
        end = q.get("fiscalDateEnding")
        if not end:
            continue
        rows.append({
            "symbol": symbol, "fiscal_date_ending": end, "reported_date": q.get("reportedDate"),
            "report_time": q.get("reportTime"), "reported_eps": _num(q.get("reportedEPS")),
            "estimated_eps": _num(q.get("estimatedEPS")), "surprise": _num(q.get("surprise")),
            "surprise_pct": _num(q.get("surprisePercentage")), "raw_path": raw_path,
            "updated_at": now,
        })  # fmt: skip
    return rows


class Prices:
    """One symbol's daily series, sorted by date."""

    def __init__(self, rows: Sequence[tuple[str, float, float]]) -> None:
        ordered = sorted(rows)
        self.days = [r[0] for r in ordered]
        self.close = [r[1] for r in ordered]
        self.adj = [r[2] for r in ordered]

    def index_on_or_before(self, day: str, max_gap: int = PRICE_GAP_DAYS) -> int | None:
        i = bisect_right(self.days, day) - 1
        if i < 0:
            return None
        gap = (date.fromisoformat(day) - date.fromisoformat(self.days[i])).days
        return i if gap <= max_gap else None

    def volatility(self, i: int) -> float | None:
        if i < MIN_RETURNS:
            return None
        window = self.adj[max(0, i - VOL_RETURNS) : i + 1]
        rets = [math.log(b / a) for a, b in itertools.pairwise(window) if a > 0 < b]
        if len(rets) < MIN_RETURNS:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(252)

    def drawdown(self, i: int) -> float | None:
        start = (date.fromisoformat(self.days[i]) - timedelta(days=365)).isoformat()
        j = bisect_right(self.days, start)
        if self.days[0] > start:  # less than a year of history
            return None
        peak, worst = 0.0, 0.0
        for x in self.adj[j : i + 1]:
            peak = max(peak, x)
            worst = min(worst, x / peak - 1)
        return worst


def _shares(conn: Connection, company_id: int, period: tuple[str, int, int],
            end: str) -> tuple[float, str, str] | None:  # fmt: skip
    """(shares outstanding at a period end, how, accession of the filing that gave it)."""
    upto = (date.fromisoformat(end) + timedelta(days=COVER_DAYS)).isoformat()
    cover = conn.execute(
        select(xbrl_fact.c.val, xbrl_fact.c.end_date, xbrl_fact.c.accession_no)
        .where(xbrl_fact.c.company_id == company_id,
               xbrl_fact.c.taxonomy == "dei",
               xbrl_fact.c.tag == "EntityCommonStockSharesOutstanding",
               xbrl_fact.c.dims == "", xbrl_fact.c.end_date >= end, xbrl_fact.c.end_date <= upto)
        .order_by(xbrl_fact.c.end_date)
    ).first()  # fmt: skip
    if cover is not None and cover[0] > 0:
        return float(cover[0]), f"cover page {cover[1]}", str(cover[2])
    days = 365 if period[0] == "FY" else 91
    diluted = conn.execute(
        select(xbrl_fact.c.val, xbrl_fact.c.start_date).where(
            xbrl_fact.c.company_id == company_id,
            xbrl_fact.c.taxonomy == "us-gaap",
            xbrl_fact.c.tag == "WeightedAverageNumberOfDilutedSharesOutstanding",
            xbrl_fact.c.dims == "", xbrl_fact.c.end_date == end)
    ).all()  # fmt: skip
    for val, start in diluted:
        if start and abs((date.fromisoformat(end) - date.fromisoformat(start)).days - days) < 20:
            return float(val), "diluted weighted-average shares", ""
    # A fiscal fourth quarter: the 10-K gives the average for the year only.
    year = [
        float(v)
        for v, s in diluted
        if s and abs((date.fromisoformat(end) - date.fromisoformat(s)).days - 365) < 20
    ]
    if period[0] == "Q" and year:
        return year[0], "diluted weighted-average shares (fiscal year)", ""
    return None


def _surprise(rows: list[Any], end: str) -> tuple[float, Any] | None:
    for r in rows:
        if abs((date.fromisoformat(r["fiscal_date_ending"]) - date.fromisoformat(end)).days) > 10:
            continue
        if r["reported_eps"] is None or not r["estimated_eps"]:
            return None
        return (r["reported_eps"] - r["estimated_eps"]) / abs(r["estimated_eps"]), r
    return None


def market_metric_rows(
    conn: Connection, company_id: int, symbol: str, codes: set[str], now: datetime
) -> list[dict[str, Any]]:
    prices = Prices(conn.execute(
        select(market_daily.c.trade_date, market_daily.c.close, market_daily.c.adjusted_close)
        .where(market_daily.c.symbol == symbol)
    ).all())  # fmt: skip
    earnings = list(conn.execute(
        select(earnings_report).where(earnings_report.c.symbol == symbol)
    ).mappings().all())  # fmt: skip
    out = []

    def put(code: str, key: tuple[str, int, int], end: str, value: float, unit: str,
            formula: str, accn: str = "") -> None:  # fmt: skip
        if code in codes:
            out.append({
                "company_id": company_id, "metric_code": code, "period_type": key[0],
                "fiscal_year": key[1], "fiscal_quarter": key[2], "period_end": end,
                "value": value, "value_status": "OK", "unit": unit, "derivation": "MARKET",
                "formula": formula[:300], "source_tags": json.dumps({"market": [SOURCE]}),
                "source_accn": accn, "computed_at": now,
            })  # fmt: skip

    for key, end in sorted(fiscal_periods(conn, company_id).items()):
        i = prices.index_on_or_before(end) if prices.days else None
        if i is not None:
            day = prices.days[i]
            shares = _shares(conn, company_id, key, end)
            if shares is not None:
                put("market_cap", key, end, prices.close[i] * shares[0], "USD",
                    f"close {prices.close[i]:,.2f} on {day} x {shares[0]:,.0f} shares "
                    f"({shares[1]})", shares[2])  # fmt: skip
            vol = prices.volatility(i)
            if vol is not None:
                put("volatility_90d", key, end, vol, "fraction",
                    f"annualized stdev of {VOL_RETURNS} daily returns to {day}")  # fmt: skip
            dd = prices.drawdown(i)
            if dd is not None:
                put("max_drawdown_52w", key, end, dd, "fraction",
                    f"largest fall from a high in the year to {day}")  # fmt: skip
        if key[0] == "Q":
            s = _surprise(earnings, end)
            if s is not None:
                r = s[1]
                put("eps_surprise_pct", key, end, s[0], "fraction",
                    f"EPS {r['reported_eps']} vs estimate {r['estimated_eps']} "
                    f"(reported {r['reported_date']})")  # fmt: skip
    return out


def run_market_stage(
    companies: Sequence[CompanyConfig],
    av: AlphaVantageClient,
    registry: Registry,
    industry: IndustryConfig,
    engine: Engine,
    *,
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    since = (today() - timedelta(days=366 * KEEP_YEARS)).isoformat()
    results: list[TaskResult] = []
    benchmark_done = False
    for cfg in companies:
        try:
            symbol = cfg.symbols()[0]
            stamp = now()
            with engine.begin() as conn:
                row = company_row(conn, cfg)
                if not benchmark_done:
                    bench, bkey = av.daily_adjusted(BENCHMARK)
                    upsert(conn, market_daily, price_rows(bench, BENCHMARK, since, bkey, stamp),
                           key_cols=["symbol", "trade_date"])  # fmt: skip
                    benchmark_done = True
                daily, dkey = av.daily_adjusted(symbol)
                prices = price_rows(daily, symbol, since, dkey, stamp)
                upsert(conn, market_daily, prices, key_cols=["symbol", "trade_date"])
                earn, ekey = av.earnings(symbol)
                upsert(conn, earnings_report, earnings_rows(earn, symbol, ekey, stamp),
                       key_cols=["symbol", "fiscal_date_ending"])  # fmt: skip
                group = industry.groups[row["industry_group"]]
                codes = MARKET_CODES & {
                    m.code for m in registry.for_company(cfg.ticker, group).metrics
                }
                rows = market_metric_rows(conn, row["company_id"], symbol, codes, stamp)
                keys = ["company_id", "metric_code", "period_type", "fiscal_year",
                        "fiscal_quarter"]  # fmt: skip
                upsert(conn, metric_value, rows, key_cols=keys)
                produced = {tuple(r[k] for k in keys[1:]) for r in rows}
                existing = conn.execute(
                    select(*(metric_value.c[k] for k in keys[1:])).where(
                        metric_value.c.company_id == row["company_id"],
                        metric_value.c.derivation == "MARKET")
                ).all()  # fmt: skip
                stale = [tuple(e) for e in existing if tuple(e) not in produced]
                if stale:
                    delete_keys(conn, metric_value, keys[1:], stale,
                                metric_value.c.company_id == row["company_id"])  # fmt: skip
            detail = f"{len(prices)} prices, {len(rows)} market values"
            log.info("%s: market %s (%d stale removed)", cfg.ticker, detail, len(stale))
            results.append(TaskResult(cfg.ticker, "market", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: market stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "market", ok=False, detail=str(exc)))
    return results
