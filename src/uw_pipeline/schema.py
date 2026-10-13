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
    Column("segment", String(200), nullable=False),  # loan class (Phase 8), e.g. "Auto"
    Column("parent_segment", String(200)),  # the firm's top-level segment, e.g. "Consumer"
    Column("outstanding", Float(53), nullable=False),  # USD
    Column("pct_of_book", Float),  # fraction of the period's total
    Column("nonaccrual", Float(53)),  # USD, when disclosed
    Column("nonaccrual_ratio", Float),  # nonaccrual / outstanding
    Column("evidence", String(1000), nullable=False),
    Column("source_doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "period_end", "segment", name="uq_loan_segment"),
)

# Left side of the Loan Portfolio Composition modal (Phase 8), generated by rules. One row per
# company: the latest period shown plus the prior year-end for comparison.
loan_portfolio_detail = Table(
    "loan_portfolio_detail",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("period_end", String(10), nullable=False),
    Column("prior_period_end", String(10)),
    Column("methodology", Text, nullable=False),
    Column("data_sources_json", Text, nullable=False),  # [{title, url, date}]
    Column("raw_data_points_json", Text, nullable=False),  # [{label, text}]
    Column("updated_at", DateTime(timezone=True), nullable=False),
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
    Column("supporting_evidence", Text),  # KEY: the model's brief statement of the facts
    # "<source title>: “<quote>”" per evidence row, built by rules (Phase 8)
    Column("supporting_evidence_text", Text),
    Column("event_date", String(10)),
    Column("updated_at", String(10)),  # event_date, else the newest date among its sources
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
    # Readable title and link of the source (Phase 8, enrich/sources.py)
    Column("source_title", String(500)),
    Column("source_url", String(2000)),
    Column("source_date", String(10)),
)

# Key insights -> the strategic/risk and behavioral insights they cite (Phase 8).
insight_link = Table(
    "insight_link",
    metadata,
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), primary_key=True),
    Column("linked_insight_id", Integer, ForeignKey("insight.insight_id"), primary_key=True),
)

insight_metric = Table(
    "insight_metric",
    metadata,
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), primary_key=True),
    Column("metric_code", String(60), primary_key=True),
    # INSIGHT (chosen with the insight) | METRIC_DETAIL (chosen by the metricdetail stage)
    Column("link_source", String(16), nullable=False, server_default="INSIGHT"),
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
    # Phase 8 (followups_v2): background facts, the risk area probed, the D&O angle
    Column("summary", Text),
    Column("following_risk", String(200)),
    Column("uw_relevance", Text),
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

followup_question_metric = Table(
    "followup_question_metric",
    metadata,
    Column("question_id", Integer, ForeignKey("followup_question.question_id"), primary_key=True),
    Column("metric_code", String(60), primary_key=True),
    Column("link_source", String(16), nullable=False, server_default="QUESTION"),
)

# Sources of a follow-up question, by rules: the evidence of its insights plus the filings its
# metrics come from (Phase 8).
followup_question_source = Table(
    "followup_question_source",
    metadata,
    Column("source_row_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "question_id",
        Integer,
        ForeignKey("followup_question.question_id"),
        nullable=False,
        index=True,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("source_kind", String(12), nullable=False),  # DOCUMENT | METRIC | SIGNAL | NEWS
    Column("title", String(500), nullable=False),
    Column("url", String(2000)),
    Column("source_date", String(10)),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("metric_code", String(60)),
)

# Metric detail modal (Phase 8, decisions D-036) -----------------------------------------------

# Rule-generated methodology per (company, metric) with data.
metric_detail = Table(
    "metric_detail",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("metric_code", String(60), primary_key=True),
    Column("methodology", Text, nullable=False),
    Column("latest_period", String(20)),
    Column("latest_value_text", String(40)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Which filing the latest quarter and latest fiscal year of a metric come from.
metric_source = Table(
    "metric_source",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("metric_code", String(60), primary_key=True),
    Column("period_type", String(2), primary_key=True),  # Q | FY
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_quarter", Integer, nullable=False),
    Column("derivation", String(20), nullable=False),
    Column("title", String(500), nullable=False),
    Column("url", String(2000)),
    Column("form_type", String(20)),
    Column("filed_at", String(10)),
    Column("accession_no", String(25)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# LLM underwriting relevance per metric; each generation is a new current set per company.
metric_insight = Table(
    "metric_insight",
    metadata,
    Column("metric_insight_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("metric_code", String(60), nullable=False),
    Column("uw_relevance", Text, nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("model", String(60), nullable=False),
    Column("prompt_version", String(40), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
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

# Data quality (Phase 7, decisions D-035) ------------------------------------------------------

# Every earnings-release value compared with the XBRL value for the same period.
release_check = Table(
    "release_check",
    metadata,
    Column("check_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("accession_no", String(25), nullable=False),
    Column("metric_code", String(60), nullable=False),
    Column("period_type", String(2), nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_quarter", Integer, nullable=False),
    Column("release_value", Float(53), nullable=False),
    Column("xbrl_value", Float(53), nullable=False),
    Column("agrees", Boolean, nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "company_id",
        "accession_no",
        "metric_code",
        "period_type",
        "fiscal_year",
        "fiscal_quarter",
        name="uq_release_check",
    ),
)

# Generated vs kept (after grounding) per LLM generation, for the drop-rate DQ rule.
llm_task_stat = Table(
    "llm_task_stat",
    metadata,
    Column("stat_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("task", String(40), nullable=False),
    Column("generated", Integer, nullable=False),
    Column("kept", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

dq_finding = Table(
    "dq_finding",
    metadata,
    Column("finding_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("pipeline_run.run_id")),  # NULL for ad-hoc checks
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("rule_id", String(40), nullable=False),
    Column("severity", String(8), nullable=False),  # Error | Warning | Info
    Column("metric_code", String(60)),
    Column("period", String(20)),
    Column("message", String(1000), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
