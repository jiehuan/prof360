from datetime import UTC, datetime

from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.jobs.run_dq import run_dq_stage
from uw_pipeline.quality.dq import (
    load_dq_config,
    negative_value,
    quarter_jump,
    quarters_vs_year,
)
from uw_pipeline.schema import (
    company,
    dq_finding,
    llm_task_stat,
    metadata,
    metric_value,
    release_check,
    signal,
    source_document,
)

CFG = load_dq_config(CONFIG_DIR)
REG = load_registry(CONFIG_DIR)
IND = load_industry_config(CONFIG_DIR)
NOW = datetime(2026, 10, 13, tzinfo=UTC)
METRICS = {m.code: m for m in REG.for_company("MAR", IND.groups["HOTELS"]).metrics}


def v(code, ptype, fy, fq, value, derivation="REPORTED"):
    return {"metric_code": code, "period_type": ptype, "fiscal_year": fy, "fiscal_quarter": fq,
            "value": value, "derivation": derivation}  # fmt: skip


def test_quarter_jump_needs_both_qoq_and_yoy_on_meaningful_base() -> None:
    values = [
        # scale error in 2026 Q2: off against both comparisons -> flagged
        v("revenue", "Q", 2025, 2, 6.7e9), v("revenue", "Q", 2026, 1, 6.6e9),
        v("revenue", "Q", 2026, 2, 7071e9),
        # seasonal Q4 spike: big vs Q3 but normal vs last Q4 -> not flagged
        v("operating_income", "Q", 2024, 4, 500e6), v("operating_income", "Q", 2025, 3, 60e6),
        v("operating_income", "Q", 2025, 4, 499e6),
        # tiny base -> not flagged
        v("net_income", "Q", 2025, 2, 1e6), v("net_income", "Q", 2026, 1, 1e6),
        v("net_income", "Q", 2026, 2, 900e6),
        # not a checked metric
        v("capex", "Q", 2025, 2, 10e6), v("capex", "Q", 2026, 1, 100e6), v("capex", "Q", 2026, 2, 9e9),
    ]  # fmt: skip
    found = quarter_jump(values, METRICS, CFG.quarter_jump)
    assert [(f.metric_code, f.period) for f in found] == [("revenue", "FY2026 Q2")]


def test_negative_value_only_for_recent_periods_of_listed_metrics() -> None:
    values = [
        v("revenue", "Q", 2026, 2, -5e6),
        v("net_income", "Q", 2026, 2, -9e9),
        v("cash", "FY", 2018, 0, -1e6),
        v("cash", "FY", 2025, 0, 1e6),
    ]
    found = negative_value(values, CFG.negative_value)
    assert [f.metric_code for f in found] == ["revenue"]


def test_quarters_must_add_up_to_year() -> None:
    values = [v("revenue", "FY", 2025, 0, 100e9)] + [
        v("revenue", "Q", 2025, q, x) for q, x in [(1, 24e9), (2, 25e9), (3, 25e9), (4, 20e9)]
    ] + [v("capex", "FY", 2025, 0, 10e9)] + [
        v("capex", "Q", 2025, q, 2.5e9) for q in (1, 2, 3, 4)
    ]  # fmt: skip
    found = quarters_vs_year(values, CFG.quarters_vs_year)
    assert [(f.metric_code, f.period) for f in found] == [("revenue", "FY2025")]


def test_dq_stage_end_to_end_and_rerun_replaces(tmp_path) -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        c.execute(
            insert(source_document).values(
                company_id=1,
                source_type="SEC_FILING",
                form_type="10-Q",
                doc_role="PRIMARY",
                accession_no="a",
                doc_name="d",
                filed_at="2026-03-01",
                url="u",
            )
        )  # 226 days old
        c.execute(insert(release_check).values(
            company_id=1, accession_no="r1", metric_code="revenue", period_type="Q",
            fiscal_year=2026, fiscal_quarter=2, release_value=7.1e9, xbrl_value=7.071e9,
            agrees=False, checked_at=NOW))  # fmt: skip
        c.execute(insert(llm_task_stat), [
            {"company_id": 1, "task": "key_insights", "generated": 10, "kept": 6, "created_at": NOW},
            {"company_id": 1, "task": "followups", "generated": 15, "kept": 15, "created_at": NOW},
        ])  # fmt: skip
        c.execute(insert(signal).values(
            signal_key="k", company_id=1, rule_id="8k_5.02_cfo_departure",
            category="Governance & Leadership", subcategory="Leadership Change",
            severity="Warning", title="Possible CFO departure (disputed)", description="d",
            event_date="2026-06-01", method="VOTE", review_status="DISPUTED",
            detected_at=NOW))  # fmt: skip
        c.execute(insert(metric_value).values(
            company_id=1, metric_code="cash", period_type="Q", fiscal_year=2026,
            fiscal_quarter=2, period_end="2026-06-30", value=-1e6, value_status="OK",
            unit="USD", derivation="REPORTED", formula="f", source_tags="{}", source_accn="a",
            computed_at=NOW))  # fmt: skip
    cfgs = [CompanyConfig(ticker="MAR", cik=1048286)]
    for _ in range(2):
        res = run_dq_stage(cfgs, REG, IND, CFG, CONFIG_DIR, engine, run_id=None, now=lambda: NOW)
        assert res[0].ok
    with engine.connect() as c:
        rules = sorted(r.rule_id for r in c.execute(select(dq_finding.c.rule_id)))
    # detail_coverage: cash has data but the metricdetail stage has not run
    assert set(rules) == {"missing_metric", "negative_value", "release_vs_xbrl", "llm_drop_rate",
                          "stale_filings", "review_queue", "detail_coverage"}  # fmt: skip
    assert (
        rules.count("llm_drop_rate") == 1 and rules.count("release_vs_xbrl") == 1
    )  # rerun replaced
