"""Phase 2 acceptance on recorded Marriott data (10-Q for the quarter ended 2026-06-30)."""

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.jobs.run_company import run_company_stage
from uw_pipeline.jobs.run_xbrl import run_compute_stage, run_facts_stage
from uw_pipeline.schema import company, metadata, metric_value, xbrl_fact

TODAY = date(2026, 10, 13)


@pytest.fixture
def mar_db(sec_routes, make_sec_client):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    cfgs = [c for c in load_companies(CONFIG_DIR) if c.ticker == "MAR"]
    industry = load_industry_config(CONFIG_DIR)
    registry = load_registry(CONFIG_DIR)
    sec = make_sec_client(day=TODAY)

    def run_all():
        results = run_company_stage(cfgs, sec, industry, engine)
        results += run_facts_stage(cfgs, sec, registry, industry, engine, today=lambda: TODAY)
        results += run_compute_stage(cfgs, registry, industry, engine)
        assert all(r.ok for r in results), results
        return results

    return engine, run_all


def _metric(engine, code, ptype, fy, fq):
    with engine.connect() as conn:
        return (
            conn.execute(
                select(metric_value).where(
                    metric_value.c.metric_code == code,
                    metric_value.c.period_type == ptype,
                    metric_value.c.fiscal_year == fy,
                    metric_value.c.fiscal_quarter == fq,
                )
            )
            .mappings()
            .one()
        )


def test_marriott_q2_2026_matches_filing(mar_db) -> None:
    engine, run_all = mar_db
    run_all()
    q2 = ("Q", 2026, 2)
    expected = {
        "revenue": 7_071e6,
        "total_debt": 16_915e6,  # 16,455 long-term + 460 current
        "long_term_debt": 16_455e6,
        "cash": 462e6,
        "equity": -4_525e6,
        "gross_fee_revenue": 1_578e6,
        "net_fee_revenue": 1_547e6,
    }
    for code, value in expected.items():
        assert _metric(engine, code, *q2)["value"] == value, code
    de = _metric(engine, "debt_to_equity", *q2)
    assert (de["value"], de["value_status"]) == (None, "NM")  # negative equity
    debt = _metric(engine, "total_debt", *q2)
    assert debt["source_accn"] == "0001048286-26-000035"


def test_q4_equals_fy_minus_first_three_quarters(mar_db) -> None:
    engine, run_all = mar_db
    run_all()
    for code in ("revenue", "operating_cash_flow", "capex"):
        fy = _metric(engine, code, "FY", 2025, 0)["value"]
        q = [_metric(engine, code, "Q", 2025, i)["value"] for i in (1, 2, 3)]
        q4 = _metric(engine, code, "Q", 2025, 4)
        assert q4["derivation"] == "DERIVED"
        assert q4["value"] == pytest.approx(fy - sum(q)), code


def test_history_depth(mar_db) -> None:
    engine, run_all = mar_db
    run_all()
    with engine.connect() as conn:
        fys = (
            conn.execute(
                select(metric_value.c.fiscal_year).where(
                    metric_value.c.metric_code == "revenue", metric_value.c.period_type == "FY"
                )
            )
            .scalars()
            .all()
        )
        qs = conn.execute(
            select(func.count()).where(
                metric_value.c.metric_code == "revenue", metric_value.c.period_type == "Q"
            )
        ).scalar_one()
    assert {2021, 2022, 2023, 2024, 2025} <= set(fys)
    assert qs >= 8


def test_rerun_is_idempotent(mar_db) -> None:
    engine, run_all = mar_db
    run_all()

    def snapshot():
        with engine.connect() as conn:
            facts = conn.execute(select(func.count()).select_from(xbrl_fact)).scalar_one()
            values = conn.execute(
                select(
                    metric_value.c.metric_code, metric_value.c.period_type,
                    metric_value.c.fiscal_year, metric_value.c.fiscal_quarter,
                    metric_value.c.value, metric_value.c.metric_value_id,
                ).order_by(metric_value.c.metric_value_id)
            ).all()  # fmt: skip
            companies = conn.execute(select(func.count()).select_from(company)).scalar_one()
        return facts, values, companies

    before = snapshot()
    results = run_all()
    assert snapshot() == before
    assert [r.detail for r in results if r.stage == "facts"] == ["unchanged"]
