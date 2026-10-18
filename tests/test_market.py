"""Market data (Phase 15a, D-050): Alpha Vantage prices and earnings, market metrics at the
company's period ends, stock-drop and earnings-miss signals. Recorded responses for Ford and SPY
(2023-01 to 2024-09) are under tests/fixtures/av/market."""

import json
from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import create_engine, insert, select
from tenacity import wait_none

from tests.conftest import CONFIG_DIR, FIXTURES, FakeClock
from uw_pipeline.clients.alphavantage import AV_URL, AlphaVantageClient
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.jobs.run_market import Prices, earnings_rows, price_rows, run_market_stage
from uw_pipeline.schema import company, metadata, metric_value, xbrl_fact
from uw_pipeline.signals.detect import market_signals
from uw_pipeline.signals.rules import load_signal_rules

MARKET = FIXTURES / "av" / "market"
NOW = datetime(2024, 9, 30, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((MARKET / name).read_text())


def series(symbol: str) -> list[tuple[str, float]]:
    rows = price_rows(load(f"daily_{symbol}.json"), symbol, "2000-01-01", "k", NOW)
    return [(r["trade_date"], r["adjusted_close"]) for r in rows]


def test_ford_july_2024_drop_and_earnings_miss_are_flagged() -> None:
    """Acceptance (PLAN 15a): Ford fell about 18% on 2024-07-25 after a Q2 EPS miss."""
    earnings = earnings_rows(load("earnings_F.json"), "F", "k", NOW)
    rules = load_signal_rules(CONFIG_DIR)
    out = market_signals(series("F"), series("SPY"), earnings, rules, "2024-09-30")
    found = {(s.rule_id, s.event_date) for s in out}
    assert ("stock_drop", "2024-07-25") in found
    assert ("earnings_miss_drop", "2024-07-24") in found
    drop = next(s for s in out if (s.rule_id, s.event_date) == ("stock_drop", "2024-07-25"))
    assert "fell 18.4%" in drop.description
    # Every flagged day meets both thresholds in the stored prices.
    f, spy = dict(series("F")), dict(series("SPY"))
    days, sdays = sorted(f), sorted(spy)
    for s in (s for s in out if s.rule_id == "stock_drop"):
        i = days.index(s.event_date)
        ret = f[days[i]] / f[days[i - 1]] - 1
        bret = spy[s.event_date] / spy[sdays[sdays.index(s.event_date) - 1]] - 1
        assert ret <= -0.10 and ret - bret <= -0.08


def test_no_signal_outside_the_lookback_or_for_a_beat() -> None:
    earnings = earnings_rows(load("earnings_F.json"), "F", "k", NOW)
    rules = load_signal_rules(CONFIG_DIR)
    late = market_signals(series("F"), series("SPY"), earnings, rules, "2028-01-01")
    assert late == []  # all older than three years
    beats = [e | {"reported_eps": 9.9} for e in earnings]
    out = market_signals(series("F"), series("SPY"), beats, rules, "2024-09-30")
    assert not any(s.rule_id == "earnings_miss_drop" for s in out)


def test_volatility_and_drawdown() -> None:
    days = [date.fromordinal(date(2024, 1, 1).toordinal() + i).isoformat() for i in range(400)]
    flat = Prices([(d, 10.0, 10.0) for d in days])
    assert flat.volatility(len(days) - 1) == 0.0 and flat.drawdown(len(days) - 1) == 0.0
    # Up to 20, then down to 15: a 25% drawdown.
    path = [10 + 0.05 * i for i in range(200)] + [20 - 0.025 * i for i in range(1, 201)]
    p = Prices([(d, x, x) for d, x in zip(days, path, strict=True)])
    assert p.drawdown(len(days) - 1) == pytest.approx(15 / 20 - 1, abs=1e-3)
    assert flat.index_on_or_before("2099-01-01") is None  # beyond the gap allowed


class FakeAV:
    def daily_adjusted(self, symbol):
        return load(f"daily_{symbol}.json"), f"av/daily/{symbol}.json"

    def earnings(self, symbol):
        return load("earnings_F.json"), f"av/earnings/{symbol}.json"


def test_market_stage_writes_metrics_at_period_ends() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(company).values(
                company_id=1,
                cik="0000037996",
                ticker="F",
                name="Ford",
                legal_name="FORD MOTOR CO",
                industry_group="AUTOMOTIVE",
                is_financial=False,
                is_active=True,
                source_url="u",
                source_hash="h",
                raw_path="p",
                last_refreshed_at=NOW,
            )
        )
        for fy, fq, ptype, end in [(2024, 2, "Q", "2024-06-30"), (2023, 0, "FY", "2023-12-31")]:
            conn.execute(
                insert(metric_value).values(
                    company_id=1,
                    metric_code="revenue",
                    period_type=ptype,
                    fiscal_year=fy,
                    fiscal_quarter=fq,
                    period_end=end,
                    value=1.0,
                    value_status="OK",
                    unit="USD",
                    derivation="REPORTED",
                    formula="revenue",
                    source_tags="{}",
                    source_accn="a",
                    computed_at=NOW,
                )
            )
        conn.execute(
            insert(xbrl_fact).values(  # the Q2 10-Q's cover-page share count
                fact_key="k1",
                company_id=1,
                taxonomy="dei",
                tag="EntityCommonStockSharesOutstanding",
                dims="",
                unit="shares",
                val=3_900_000_000.0,
                end_date="2024-07-19",
                form="10-Q",
                accession_no="0000037996-24-000040",
                filed_at="2024-07-24",
                source="companyfacts",
            )
        )
    cfg = CompanyConfig(ticker="F", cik=37996)
    res = run_market_stage([cfg], FakeAV(), load_registry(CONFIG_DIR),
                           load_industry_config(CONFIG_DIR), engine,
                           today=lambda: date(2024, 9, 30), now=lambda: NOW)  # fmt: skip
    assert res[0].ok, res
    with engine.connect() as conn:
        rows = {
            (r.metric_code, r.period_type): r
            for r in conn.execute(select(metric_value).where(metric_value.c.derivation == "MARKET"))
        }
    close = float(load("daily_F.json")["Time Series (Daily)"]["2024-06-28"]["4. close"])
    cap = rows[("market_cap", "Q")]
    assert cap.value == pytest.approx(close * 3_900_000_000.0)
    assert cap.source_accn == "0000037996-24-000040" and "2024-06-28" in cap.formula
    assert 0.1 < rows[("volatility_90d", "Q")].value < 1.5
    assert -1 < rows[("max_drawdown_52w", "Q")].value <= 0
    q2 = next(e for e in load("earnings_F.json")["quarterlyEarnings"]
              if e["fiscalDateEnding"] == "2024-06-30")  # fmt: skip
    rep, est = float(q2["reportedEPS"]), float(q2["estimatedEPS"])
    assert rows[("eps_surprise_pct", "Q")].value == pytest.approx((rep - est) / abs(est))
    assert ("eps_surprise_pct", "FY") not in rows  # quarters only


def test_client_requests_full_adjusted_history_and_lands_it(tmp_path) -> None:
    clock = FakeClock()
    av = AlphaVantageClient("TESTKEY123", LocalStorage(tmp_path), wait=wait_none(),
                            rate_limiter=TokenBucket(rate=5, clock=clock, sleep=clock.sleep),
                            today=lambda: date(2026, 10, 17))  # fmt: skip
    with respx.mock() as router:
        route = router.get(AV_URL).mock(return_value=httpx.Response(200, json=load("daily_F.json")))
        payload, key = av.daily_adjusted("F")
    params = route.calls[0].request.url.params
    assert (params["function"], params["outputsize"]) == ("TIME_SERIES_DAILY_ADJUSTED", "full")
    assert key == "av/daily/F/2026-10-17.json" and (tmp_path / key).exists()
    assert "TESTKEY123" not in key and payload["Time Series (Daily)"]
