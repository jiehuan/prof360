"""SQL Server integration (Phase 12, D-040): the pipeline and application tables, the MERGE
upsert path and the main stages on a real SQL Server. Skipped unless MSSQL_TEST_URL is set;
`scripts/test_mssql.sh` runs them against a local SQL Server container.

The pipeline and app tables go to separate schemas, as on Azure SQL.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, insert, inspect, select, text

from tests import test_insights as ti
from tests import test_metric_detail as tm
from uw_pipeline.clients.db import create_tables, make_engine
from uw_pipeline.load.upsert import delete_keys, insert_missing, upsert
from uw_pipeline.schema import company, metadata, metric_value, signal
from uw_pipeline.schema_app import KeyInsights, app_metadata

URL = os.environ.get("MSSQL_TEST_URL")
pytestmark = [pytest.mark.mssql,
              pytest.mark.skipif(not URL, reason="MSSQL_TEST_URL not set")]  # fmt: skip
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _fresh(schema: str, md) -> Engine:
    assert URL is not None
    engine = make_engine(URL, schema)
    with engine.begin() as c:
        if inspect(c).has_schema(schema):
            md.drop_all(c)
    create_tables(engine, md)
    return engine


@pytest.fixture
def pipe() -> Engine:
    return _fresh("uwt_pipeline", metadata)


@pytest.fixture
def app() -> Engine:
    return _fresh("uwt_app", app_metadata)


def _company(c) -> None:
    c.execute(insert(company).values(
        company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
        industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
        source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip


def test_tables_live_in_their_schema_with_unicode_text(pipe: Engine, app: Engine) -> None:
    with pipe.connect() as c:
        tables = set(inspect(c).get_table_names(schema="uwt_pipeline"))
        types = {col["name"]: str(col["type"])
                 for col in inspect(c).get_columns("signal", schema="uwt_pipeline")}  # fmt: skip
    assert {"metric_value", "insight", "pipeline_task", "llm_call"} <= tables
    assert types["title"].startswith("NVARCHAR") and types["snippet"].startswith("NVARCHAR")
    with app.connect() as c:
        assert {"ItemLink", "CompanyMetricDetail", "KeyInsights"} <= set(
            inspect(c).get_table_names(schema="uwt_app"))  # fmt: skip
    with pipe.begin() as c:
        _company(c)
        quote = "Chief Executive Officer\u2019s \u201cremarks\u201d \u2014 \u4e2d\u6587 \u20ac"
        c.execute(insert(signal).values(
            signal_key="k", company_id=1, rule_id="r", category="c", subcategory="s",
            severity="Info", title=quote, description="d", event_date="2026-10-01",
            method="RULE", review_status="RULE_ONLY", detected_at=NOW))  # fmt: skip
    with pipe.connect() as c:
        assert c.execute(select(signal.c.title)).scalar_one() == quote


def test_merge_upsert_insert_missing_and_large_key_delete(pipe: Engine) -> None:
    row = dict(company_id=1, metric_code="revenue", period_type="Q", fiscal_quarter=1,
               period_end="2026-03-31", value_status="OK", unit="USD", derivation="REPORTED",
               formula="f", source_tags="{}", source_accn="a", computed_at=NOW)  # fmt: skip
    keys = ["company_id", "metric_code", "period_type", "fiscal_year", "fiscal_quarter"]
    rows = [row | {"fiscal_year": y, "value": float(y)} for y in range(1000, 3500)]
    with pipe.begin() as c:
        _company(c)
        upsert(c, metric_value, rows, key_cols=keys)
        upsert(c, metric_value, [rows[0] | {"value": -1.0}], key_cols=keys)  # update
        insert_missing(c, metric_value, [rows[1] | {"value": -2.0}], key_cols=keys)  # kept
    with pipe.connect() as c:
        vals = dict(c.execute(select(metric_value.c.fiscal_year, metric_value.c.value)).all())
    assert len(vals) == 2500 and vals[1000] == -1.0 and vals[1001] == 1001.0
    with pipe.begin() as c:  # 2,400 keys x 4 columns: far over SQL Server's 2,100 parameters
        n = delete_keys(c, metric_value, keys[1:],
                        [("revenue", "Q", y, 1) for y in range(1000, 3400)],
                        metric_value.c.company_id == 1)  # fmt: skip
    assert n == 2400
    with pipe.connect() as c:
        assert c.execute(select(func.count()).select_from(metric_value)).scalar_one() == 100


def test_insights_metric_detail_publish_and_dq(pipe: Engine, app: Engine) -> None:
    """The main LLM and publish stages on SQL Server, with the unit tests' data and fake LLM."""
    from tests.conftest import CONFIG_DIR
    from tests.fake_llm import FakeLLM
    from uw_pipeline.compute.registry import load_registry
    from uw_pipeline.config import CompanyConfig, load_industry_config
    from uw_pipeline.enrich.sources import load_source_labels
    from uw_pipeline.jobs.run_dq import run_dq_stage
    from uw_pipeline.jobs.run_publish import load_publish_config, run_publish_stage
    from uw_pipeline.quality.dq import load_dq_config

    ti._seed(pipe)
    llm = FakeLLM(tm.answer)
    ti._run(pipe, llm)
    assert "relevance=1/3" in tm._run(pipe, llm)
    cfgs = [CompanyConfig(ticker="MAR", cik=1048286)]
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)

    def publish() -> str:
        res = run_publish_stage(
            cfgs, reg, ind, load_publish_config(CONFIG_DIR), load_source_labels(CONFIG_DIR),
            pipe, app, now=lambda: NOW,
        )  # fmt: skip
        assert res[0].ok, res
        return res[0].detail

    assert "written" in publish()
    assert publish().startswith("0 written")  # idempotent on SQL Server too
    with app.connect() as c:
        assert c.execute(select(func.count()).select_from(KeyInsights)).scalar_one() == 1
    calls = len(llm.calls)
    assert ti._run(pipe, llm).count("unchanged") == 4 and len(llm.calls) == calls
    res = run_dq_stage(cfgs, reg, ind, load_dq_config(CONFIG_DIR), CONFIG_DIR, pipe, run_id=None,
                       now=lambda: NOW)  # fmt: skip
    assert res[0].ok, res
    with pipe.connect() as c:
        assert c.execute(text("SELECT SCHEMA_NAME()")).scalar() is not None


def test_loan_book_stage(pipe: Engine, tmp_path) -> None:
    """Text metrics and the loan book (delete_keys, timestamps) on SQL Server."""
    from tests import test_text_metrics as tt

    tt.create_engine = lambda url: pipe  # reuse the test's seeding and assertions unchanged
    try:
        with pipe.begin() as c:
            metadata.drop_all(c)
        create_tables(pipe, metadata)
        tt.test_loan_book_by_class_with_prior_year_end(tmp_path)
    finally:
        from sqlalchemy import create_engine

        tt.create_engine = create_engine
