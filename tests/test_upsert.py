from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mssql

from uw_pipeline.load.upsert import build_merge_sql, upsert
from uw_pipeline.schema import company, metadata


def _row(**kw):
    base = dict(
        cik="0000000001",
        ticker="AAA",
        name="A",
        legal_name="A INC",
        sic_code="7011",
        sic_description="x",
        industry_group="HOTELS",
        is_financial=False,
        hq_city=None,
        hq_state=None,
        hq_country="US",
        fiscal_year_end="1231",
        exchange=None,
        state_of_incorporation=None,
        is_active=True,
        source_url="u",
        source_hash="h",
        raw_path="p",
        last_refreshed_at=datetime(2026, 1, 1),
    )
    return base | kw


def test_sqlite_upsert_is_idempotent_and_updates() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        upsert(conn, company, [_row(), _row(cik="0000000002", ticker="BBB")], ["cik"])
        upsert(conn, company, [_row(name="A renamed")], ["cik"])
    with engine.connect() as conn:
        rows = conn.execute(select(company.c.company_id, company.c.cik, company.c.name)).all()
    assert sorted(rows) == [(1, "0000000001", "A renamed"), (2, "0000000002", "A")]


def test_mssql_merge_sql() -> None:
    sql = build_merge_sql(
        mssql.dialect().identifier_preparer,
        company,
        "#stg_company",
        ["cik", "ticker", "name"],
        ["cik"],
    )
    assert sql == (
        "MERGE company WITH (HOLDLOCK) AS t USING #stg_company AS s ON t.cik = s.cik "
        "WHEN MATCHED THEN UPDATE SET t.ticker = s.ticker, t.name = s.name "
        "WHEN NOT MATCHED BY TARGET THEN INSERT (cik, ticker, name) "
        "VALUES (s.cik, s.ticker, s.name);"
    )
