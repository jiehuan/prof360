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
    Text,
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
    # REPORTED | DERIVED | COMPUTED (compute stage) | LLM_EXTRACTED | PRELIMINARY (text stage)
    Column("derivation", String(20), nullable=False),
    Column("formula", String(300), nullable=False),
    Column("source_tags", String(2000), nullable=False),
    Column("source_accn", String(25), nullable=False),
    # Verbatim source quote for LLM_EXTRACTED / PRELIMINARY values (decisions D-032).
    Column("evidence", String(1000)),
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

# Every SEC document the pipeline references: filings (metadata only for most 8-Ks), primary
# documents it downloaded, and exhibits such as the EX-99.1 earnings release.
source_document = Table(
    "source_document",
    metadata,
    Column("doc_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("source_type", String(20), nullable=False),  # SEC_FILING | NEWS | TRANSCRIPT
    Column("form_type", String(20), nullable=False),
    Column("doc_role", String(20), nullable=False),  # PRIMARY | EX-99.1
    Column("accession_no", String(25), nullable=False),
    Column("doc_name", String(200), nullable=False),
    Column("items", String(100)),  # 8-K item numbers
    Column("filed_at", String(10), nullable=False),
    Column("period_end", String(10)),
    Column("url", String(500), nullable=False),
    Column("content_hash", String(64)),  # NULL until the document is downloaded
    Column("raw_path", String(500)),
    Column("fetched_at", DateTime(timezone=True)),
    UniqueConstraint("accession_no", "doc_name", name="uq_source_document"),
)

filing_section = Table(
    "filing_section",
    metadata,
    Column("section_id", Integer, primary_key=True, autoincrement=True),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("section_code", String(20), nullable=False),
    Column("start_offset", Integer, nullable=False),  # in the document's plain text
    Column("char_count", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("text_hash", String(64), nullable=False),
    UniqueConstraint("doc_id", "section_code", name="uq_filing_section"),
)

# Paragraph-aligned chunks for LLM input and citation (offsets are within the section text).
section_chunk = Table(
    "section_chunk",
    metadata,
    Column("chunk_id", Integer, primary_key=True, autoincrement=True),
    Column("section_id", Integer, ForeignKey("filing_section.section_id"), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("start_offset", Integer, nullable=False),
    Column("end_offset", Integer, nullable=False),
    Column("text", Text, nullable=False),
    UniqueConstraint("section_id", "chunk_index", name="uq_section_chunk"),
)

# Rule-based signals (no LLM). One row per (rule, company, evidence source).
signal = Table(
    "signal",
    metadata,
    Column("signal_id", Integer, primary_key=True, autoincrement=True),
    Column("signal_key", String(40), nullable=False, unique=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("rule_id", String(60), nullable=False),
    Column("category", String(60), nullable=False),
    Column("subcategory", String(100), nullable=False),
    Column("severity", String(10), nullable=False),  # Info | Warning | Critical
    Column("title", String(300), nullable=False),
    Column("description", String(2000), nullable=False),
    Column("event_date", String(10), nullable=False),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("accession_no", String(25)),
    Column("section_code", String(20)),
    Column("snippet", String(2000)),
    # How the signal was decided (decisions D-028): RULE | VOTE
    Column("method", String(10), nullable=False, server_default="RULE"),
    # RULE_ONLY | UNANIMOUS | MAJORITY | DISPUTED
    Column("review_status", String(12), nullable=False, server_default="RULE_ONLY"),
    Column("votes_json", String(500)),
    Column("detected_at", DateTime(timezone=True), nullable=False),
)

# Every LLM call, keyed by a hash of (model, prompt version, inputs). Unchanged inputs reuse the
# stored output, so reruns make no LLM calls.
llm_call = Table(
    "llm_call",
    metadata,
    Column("input_hash", String(64), primary_key=True),
    Column("task", String(40), nullable=False),
    Column("prompt_version", String(40), nullable=False),
    Column("model", String(60), nullable=False),  # requested model
    Column("served_model", String(60)),  # differs after a server-side fallback
    Column("output_json", Text),
    Column("error", String(500)),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# One row per judgment question (document x topic) with each voter's answer (decisions D-028).
signal_vote = Table(
    "signal_vote",
    metadata,
    Column("vote_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("topic", String(40), nullable=False),
    Column("rule_value", String(30), nullable=False),
    Column("extractor_value", String(30)),
    Column("tiebreak_value", String(30)),
    Column("outcome", String(30), nullable=False),
    Column("status", String(12), nullable=False),
    Column("evidence", String(2000)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("doc_id", "topic", name="uq_signal_vote"),
)

# De-duplicated news: one row per event/story (decisions D-029). `summary` and `uw_relevance`
# are written by the LLM enrichment (Phase 5).
activity = Table(
    "activity",
    metadata,
    Column("activity_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("title", String(1000), nullable=False),
    Column("published_at", DateTime, nullable=False),
    Column("source_name", String(200), nullable=False),
    Column("url", String(2000), nullable=False),
    Column("url_hash", String(64), nullable=False),
    Column("av_summary", Text),
    Column("av_sentiment_score", Float),
    Column("av_sentiment_label", String(40)),
    Column("av_relevance_score", Float),
    Column("source_count", Integer, nullable=False),  # articles grouped into this activity
    Column("summary", Text),
    Column("uw_relevance", Text),
    Column("llm_sentiment", String(10)),
    Column("summary_input_hash", String(64)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "url_hash", name="uq_activity_canonical"),
)

# Every relevant article fetched, linked to the activity it was grouped into.
news_item = Table(
    "news_item",
    metadata,
    Column("news_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("activity_id", Integer, ForeignKey("activity.activity_id"), nullable=False),
    Column("url_hash", String(64), nullable=False),
    Column("url", String(2000), nullable=False),
    Column("title", String(1000), nullable=False),
    Column("summary", Text),
    Column("source", String(200)),
    Column("source_domain", String(200)),
    Column("authors", String(500)),
    Column("published_at", DateTime, nullable=False),
    Column("relevance", Float, nullable=False),
    Column("ticker_sentiment_score", Float),
    Column("ticker_sentiment_label", String(40)),
    Column("overall_sentiment_score", Float),
    Column("overall_sentiment_label", String(40)),
    # FIRST (the activity's canonical article) | NEAR_DUP (syndicated copy) | LLM_EVENT
    Column("dedup_method", String(12), nullable=False),
    Column("raw_path", String(500), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "url_hash", name="uq_news_item_url"),
)

# Loan book by segment for banks (FI module), extracted from MD&A text (decisions D-032).
loan_portfolio_segment = Table(
    "loan_portfolio_segment",
    metadata,
    Column("segment_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("period_end", String(10), nullable=False),
    Column("segment", String(200), nullable=False),
    Column("outstanding", Float(53), nullable=False),  # USD
    Column("pct_of_book", Float),  # fraction of the segments' total
    Column("nonaccrual", Float(53)),  # USD, when disclosed
    Column("evidence", String(1000), nullable=False),
    Column("source_doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "period_end", "segment", name="uq_loan_segment"),
)

# LLM insights (Phase 5b, decisions D-033). Each generation is a new set of rows; the newest set
# per (company, kind) has is_current = 1 and older sets are kept for audit.
insight = Table(
    "insight",
    metadata,
    Column("insight_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("kind", String(20), nullable=False),  # KEY | STRATEGIC_RISK | BEHAVIORAL
    Column("ordinal", Integer, nullable=False),  # order within its set
    Column("category", String(60)),
    Column("subcategory", String(100)),
    Column("insight_type", String(10)),  # KEY: Warning | Negative | Positive | Info
    Column("title", String(300), nullable=False),
    Column("summary", Text, nullable=False),
    Column("uw_relevance", Text),
    Column("supporting_evidence", Text),
    Column("event_date", String(10)),
    Column("is_current", Boolean, nullable=False),
    Column("model", String(60), nullable=False),
    Column("prompt_version", String(40), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

insight_evidence = Table(
    "insight_evidence",
    metadata,
    Column("evidence_id", Integer, primary_key=True, autoincrement=True),
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), nullable=False, index=True),
    Column("source_kind", String(12), nullable=False),  # DOCUMENT | METRIC | SIGNAL | NEWS
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("section_code", String(20)),
    Column("metric_code", String(60)),
    Column("signal_id", Integer),
    Column("activity_id", Integer),
    Column("snippet", String(1000), nullable=False),
)

insight_metric = Table(
    "insight_metric",
    metadata,
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), primary_key=True),
    Column("metric_code", String(60), primary_key=True),
)

followup_question = Table(
    "followup_question",
    metadata,
    Column("question_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("ordinal", Integer, nullable=False),
    Column("category", String(20), nullable=False),
    Column("question", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("system_priority", String(10), nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("model", String(60), nullable=False),
    Column("prompt_version", String(40), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

followup_question_insight = Table(
    "followup_question_insight",
    metadata,
    Column("question_id", Integer, ForeignKey("followup_question.question_id"), primary_key=True),
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), primary_key=True),
)

# Orchestration (Phase 6, decisions D-034). One row per run; one task per (company, stage).
pipeline_run = Table(
    "pipeline_run",
    metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("job", String(40), nullable=False),  # nightly | morning | news | manual
    Column("trigger", String(20), nullable=False),  # cron | manual
    Column("status", String(12), nullable=False),  # RUNNING | SUCCEEDED | PARTIAL | FAILED
    Column("stages", String(500), nullable=False),  # JSON list
    Column("tickers", String(2000), nullable=False),  # JSON list
    Column("starts", Integer, nullable=False),  # 1 + number of resumes
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("stats_json", Text),
)

pipeline_task = Table(
    "pipeline_task",
    metadata,
    Column("task_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("pipeline_run.run_id"), nullable=False, index=True),
    Column("ticker", String(16), nullable=False),
    Column("stage", String(20), nullable=False),
    Column("ordinal", Integer, nullable=False),  # execution order within the run
    # PENDING | RUNNING | SUCCEEDED | FAILED | SKIPPED
    Column("status", String(10), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("detail", String(2000)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("run_id", "ticker", "stage", name="uq_pipeline_task"),
)
