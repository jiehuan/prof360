"""Table definitions owned by the pipeline (draft per PLAN §8, pending the BFF contract)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

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

# Last processed content per (company, source), for incremental runs.
ingest_state = Table(
    "ingest_state",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("source_key", String(100), primary_key=True),
    Column("content_hash", String(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Raw XBRL facts for the tags the pipeline uses (audit trail; full JSON stays in raw storage).
xbrl_fact = Table(
    "xbrl_fact",
    metadata,
    Column("fact_id", Integer, primary_key=True, autoincrement=True),
    Column("fact_key", String(40), nullable=False, unique=True),  # sha1 of the natural key
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("taxonomy", String(40), nullable=False),
    Column("tag", String(255), nullable=False),
    Column("dims", String(1000), nullable=False),  # '' = no dimensions
    Column("unit", String(40), nullable=False),
    Column("val", Float(53), nullable=False),
    Column("start_date", String(10)),
    Column("end_date", String(10), nullable=False),
    Column("fy", Integer),
    Column("fp", String(4)),
    Column("form", String(20), nullable=False),
    Column("accession_no", String(25), nullable=False),
    Column("filed_at", String(10), nullable=False),
    Column("frame", String(20)),
    Column("source", String(20), nullable=False),  # companyfacts | instance
)

metric_definition = Table(
    "metric_definition",
    metadata,
    Column("metric_code", String(60), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("description", String(500)),
    Column("section", String(40), nullable=False),
    Column("unit", String(20), nullable=False),
    Column("applies_to", String(200), nullable=False),
    Column("is_display", Boolean, nullable=False),
    Column("company_specific", Boolean, nullable=False),
)

metric_value = Table(
    "metric_value",
    metadata,
    Column("metric_value_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("metric_code", String(60), nullable=False),
    Column("period_type", String(2), nullable=False),  # Q | FY
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_quarter", Integer, nullable=False),  # 1-4; 0 for FY
    Column("period_end", String(10), nullable=False),
    Column("value", Float(53)),  # NULL when value_status = NM
    Column("value_status", String(4), nullable=False),  # OK | NM (not meaningful)
    Column("unit", String(20), nullable=False),
    Column("derivation", String(20), nullable=False),  # REPORTED | DERIVED | COMPUTED
    Column("formula", String(300), nullable=False),
    Column("source_tags", String(2000), nullable=False),
    Column("source_accn", String(25), nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "company_id",
        "metric_code",
        "period_type",
        "fiscal_year",
        "fiscal_quarter",
        name="uq_metric_value_period",
    ),
)
