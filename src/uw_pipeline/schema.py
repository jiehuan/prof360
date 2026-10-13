"""Table definitions owned by the pipeline (draft per PLAN §8, pending the BFF contract)."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table

metadata = MetaData()

company = Table(
    "company",
    metadata,
    Column("company_id", Integer, primary_key=True, autoincrement=True),
    Column("cik", String(10), nullable=False, unique=True),
    Column("ticker", String(16), nullable=False),
    Column("name", String(200), nullable=False),
    Column("legal_name", String(200), nullable=False),
    Column("sic_code", String(4)),
    Column("sic_description", String(200)),
    Column("industry_group", String(40), nullable=False),
    Column("is_financial", Boolean, nullable=False),
    Column("hq_city", String(100)),
    Column("hq_state", String(10)),
    Column("hq_country", String(60)),
    Column("fiscal_year_end", String(4)),  # MMDD, as SEC reports it
    Column("exchange", String(20)),
    Column("state_of_incorporation", String(10)),
    Column("is_active", Boolean, nullable=False),
    Column("source_url", String(500), nullable=False),
    Column("source_hash", String(64), nullable=False),
    Column("raw_path", String(500), nullable=False),
    Column("last_refreshed_at", DateTime(timezone=True), nullable=False),
)
