"""Table definitions owned by the pipeline (draft per PLAN §8, pending the BFF contract).

Strings are Unicode (NVARCHAR on SQL Server): filing text has curly quotes and non-ASCII names.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Unicode,
    UnicodeText,
    UniqueConstraint,
)

metadata = MetaData()

company = Table(
    "company",
    metadata,
    Column("company_id", Integer, primary_key=True, autoincrement=True),
    Column("cik", Unicode(10), nullable=False, unique=True),
    Column("ticker", Unicode(16), nullable=False),
    Column("name", Unicode(200), nullable=False),
    Column("legal_name", Unicode(200), nullable=False),
    Column("sic_code", Unicode(4)),
    Column("sic_description", Unicode(200)),
    Column("industry_group", Unicode(40), nullable=False),
    Column("is_financial", Boolean, nullable=False),
    Column("hq_city", Unicode(100)),
    Column("hq_state", Unicode(10)),
    Column("hq_country", Unicode(60)),
    Column("fiscal_year_end", Unicode(4)),  # MMDD, as SEC reports it
    Column("exchange", Unicode(20)),
    Column("state_of_incorporation", Unicode(10)),
    Column("is_active", Boolean, nullable=False),
    Column("source_url", Unicode(500), nullable=False),
    Column("source_hash", Unicode(64), nullable=False),
    Column("raw_path", Unicode(500), nullable=False),
    Column("last_refreshed_at", DateTime(timezone=True), nullable=False),
)

# Last processed content per (company, source), for incremental runs.
ingest_state = Table(
    "ingest_state",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("source_key", Unicode(100), primary_key=True),
    Column("content_hash", Unicode(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Raw XBRL facts for the tags the pipeline uses (audit trail; full JSON stays in raw storage).
xbrl_fact = Table(
    "xbrl_fact",
    metadata,
    Column("fact_id", Integer, primary_key=True, autoincrement=True),
    Column("fact_key", Unicode(40), nullable=False, unique=True),  # sha1 of the natural key
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("taxonomy", Unicode(40), nullable=False),
    Column("tag", Unicode(255), nullable=False),
    Column("dims", Unicode(1000), nullable=False),  # '' = no dimensions
    Column("unit", Unicode(40), nullable=False),
    Column("val", Float(53), nullable=False),
    Column("start_date", Unicode(10)),
    Column("end_date", Unicode(10), nullable=False),
    Column("fy", Integer),
    Column("fp", Unicode(4)),
    Column("form", Unicode(20), nullable=False),
    Column("accession_no", Unicode(25), nullable=False),
    Column("filed_at", Unicode(10), nullable=False),
    Column("frame", Unicode(20)),
    Column("source", Unicode(20), nullable=False),  # companyfacts | instance
)

metric_definition = Table(
    "metric_definition",
    metadata,
    Column("metric_code", Unicode(60), primary_key=True),
    Column("display_name", Unicode(120), nullable=False),
    Column("description", Unicode(500)),
    Column("section", Unicode(40), nullable=False),
    Column("unit", Unicode(20), nullable=False),
    Column("applies_to", Unicode(200), nullable=False),
    Column("is_display", Boolean, nullable=False),
    Column("company_specific", Boolean, nullable=False),
)

metric_value = Table(
    "metric_value",
    metadata,
    Column("metric_value_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("metric_code", Unicode(60), nullable=False),
    Column("period_type", Unicode(2), nullable=False),  # Q | FY
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_quarter", Integer, nullable=False),  # 1-4; 0 for FY
    Column("period_end", Unicode(10), nullable=False),
    Column("value", Float(53)),  # NULL when value_status = NM
    Column("value_status", Unicode(4), nullable=False),  # OK | NM (not meaningful)
    Column("unit", Unicode(20), nullable=False),
    # REPORTED | DERIVED | COMPUTED (compute stage) | LLM_EXTRACTED | PRELIMINARY (text stage)
    Column("derivation", Unicode(20), nullable=False),
    Column("formula", Unicode(300), nullable=False),
    Column("source_tags", Unicode(2000), nullable=False),
    Column("source_accn", Unicode(25), nullable=False),
    # Verbatim source quote for LLM_EXTRACTED / PRELIMINARY values (decisions D-032).
    Column("evidence", Unicode(1000)),
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
    Column("source_type", Unicode(20), nullable=False),  # SEC_FILING | NEWS | TRANSCRIPT
    Column("form_type", Unicode(20), nullable=False),
    Column("doc_role", Unicode(20), nullable=False),  # PRIMARY | EX-99.1
    Column("accession_no", Unicode(25), nullable=False),
    Column("doc_name", Unicode(200), nullable=False),
    Column("items", Unicode(100)),  # 8-K item numbers
    Column("filed_at", Unicode(10), nullable=False),
    Column("period_end", Unicode(10)),
    Column("url", Unicode(500), nullable=False),
    Column("content_hash", Unicode(64)),  # NULL until the document is downloaded
    Column("raw_path", Unicode(500)),
    Column("fetched_at", DateTime(timezone=True)),
    UniqueConstraint("accession_no", "doc_name", name="uq_source_document"),
)

filing_section = Table(
    "filing_section",
    metadata,
    Column("section_id", Integer, primary_key=True, autoincrement=True),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("section_code", Unicode(20), nullable=False),
    Column("start_offset", Integer, nullable=False),  # in the document's plain text
    Column("char_count", Integer, nullable=False),
    Column("text", UnicodeText, nullable=False),
    Column("text_hash", Unicode(64), nullable=False),
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
    Column("text", UnicodeText, nullable=False),
    UniqueConstraint("section_id", "chunk_index", name="uq_section_chunk"),
)

# Rule-based signals (no LLM). One row per (rule, company, evidence source).
signal = Table(
    "signal",
    metadata,
    Column("signal_id", Integer, primary_key=True, autoincrement=True),
    Column("signal_key", Unicode(40), nullable=False, unique=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("rule_id", Unicode(60), nullable=False),
    Column("category", Unicode(60), nullable=False),
    Column("subcategory", Unicode(100), nullable=False),
    Column("severity", Unicode(10), nullable=False),  # Info | Warning | Critical
    Column("title", Unicode(300), nullable=False),
    Column("description", Unicode(2000), nullable=False),
    Column("event_date", Unicode(10), nullable=False),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("accession_no", Unicode(25)),
    Column("section_code", Unicode(20)),
    Column("snippet", Unicode(2000)),
    # How the signal was decided (decisions D-028): RULE | VOTE
    Column("method", Unicode(10), nullable=False, server_default="RULE"),
    # RULE_ONLY | UNANIMOUS | MAJORITY | DISPUTED
    Column("review_status", Unicode(12), nullable=False, server_default="RULE_ONLY"),
    Column("votes_json", Unicode(500)),
    Column("detected_at", DateTime(timezone=True), nullable=False),
)

# Every LLM call, keyed by a hash of (model, prompt version, inputs). Unchanged inputs reuse the
# stored output, so reruns make no LLM calls.
llm_call = Table(
    "llm_call",
    metadata,
    Column("input_hash", Unicode(64), primary_key=True),
    Column("task", Unicode(40), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("model", Unicode(60), nullable=False),  # requested model
    Column("served_model", Unicode(60)),  # differs after a server-side fallback
    Column("output_json", UnicodeText),
    Column("error", Unicode(500)),
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
    Column("topic", Unicode(40), nullable=False),
    Column("rule_value", Unicode(30), nullable=False),
    Column("extractor_value", Unicode(30)),
    Column("tiebreak_value", Unicode(30)),
    Column("outcome", Unicode(30), nullable=False),
    Column("status", Unicode(12), nullable=False),
    Column("evidence", Unicode(2000)),
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
    Column("title", Unicode(1000), nullable=False),
    Column("published_at", DateTime, nullable=False),
    Column("source_name", Unicode(200), nullable=False),
    Column("url", Unicode(2000), nullable=False),
    Column("url_hash", Unicode(64), nullable=False),
    Column("av_summary", UnicodeText),
    Column("av_sentiment_score", Float),
    Column("av_sentiment_label", Unicode(40)),
    Column("av_relevance_score", Float),
    Column("source_count", Integer, nullable=False),  # articles grouped into this activity
    Column("summary", UnicodeText),
    Column("uw_relevance", UnicodeText),
    Column("llm_sentiment", Unicode(10)),
    Column("summary_input_hash", Unicode(64)),
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
    Column("url_hash", Unicode(64), nullable=False),
    Column("url", Unicode(2000), nullable=False),
    Column("title", Unicode(1000), nullable=False),
    Column("summary", UnicodeText),
    Column("source", Unicode(200)),
    Column("source_domain", Unicode(200)),
    Column("authors", Unicode(500)),
    Column("published_at", DateTime, nullable=False),
    Column("relevance", Float, nullable=False),
    Column("ticker_sentiment_score", Float),
    Column("ticker_sentiment_label", Unicode(40)),
    Column("overall_sentiment_score", Float),
    Column("overall_sentiment_label", Unicode(40)),
    # FIRST (the activity's canonical article) | NEAR_DUP (syndicated copy) | LLM_EVENT
    Column("dedup_method", Unicode(12), nullable=False),
    # LLM relevance verdict (Phase 9): SUBJECT | PASSING | ANALYST | HOLDING | MARKET |
    # UNCLASSIFIED (no usable answer; kept). NULL = not judged yet.
    Column("relevance_verdict", Unicode(12)),
    Column("raw_path", Unicode(500), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "url_hash", name="uq_news_item_url"),
)

# Loan book by segment for banks (FI module), extracted from MD&A text (decisions D-032).
loan_portfolio_segment = Table(
    "loan_portfolio_segment",
    metadata,
    Column("segment_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("period_end", Unicode(10), nullable=False),
    Column("segment", Unicode(200), nullable=False),  # loan class (Phase 8), e.g. "Auto"
    Column("parent_segment", Unicode(200)),  # the firm's top-level segment, e.g. "Consumer"
    Column("outstanding", Float(53), nullable=False),  # USD
    Column("pct_of_book", Float),  # fraction of the period's total
    Column("nonaccrual", Float(53)),  # USD, when disclosed
    Column("nonaccrual_ratio", Float),  # nonaccrual / outstanding
    Column("evidence", Unicode(1000), nullable=False),
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
    Column("period_end", Unicode(10), nullable=False),
    Column("prior_period_end", Unicode(10)),
    Column("methodology", UnicodeText, nullable=False),
    Column("data_sources_json", UnicodeText, nullable=False),  # [{title, url, date}]
    Column("raw_data_points_json", UnicodeText, nullable=False),  # [{label, text}]
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# LLM insights (Phase 5b, decisions D-033). Each generation is a new set of rows; the newest set
# per (company, kind) has is_current = 1 and older sets are kept for audit.
insight = Table(
    "insight",
    metadata,
    Column("insight_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("kind", Unicode(20), nullable=False),  # KEY | STRATEGIC_RISK | BEHAVIORAL
    Column("ordinal", Integer, nullable=False),  # order within its set
    Column("category", Unicode(60)),
    Column("subcategory", Unicode(100)),
    Column("insight_type", Unicode(10)),  # KEY: Warning | Negative | Positive | Info
    Column("title", Unicode(300), nullable=False),
    Column("summary", UnicodeText, nullable=False),
    Column("uw_relevance", UnicodeText),
    Column("supporting_evidence", UnicodeText),  # KEY: the model's brief statement of the facts
    # "<source title>: “<quote>”" per evidence row, built by rules (Phase 8)
    Column("supporting_evidence_text", UnicodeText),
    Column("event_date", Unicode(10)),
    Column("updated_at", Unicode(10)),  # event_date, else the newest date among its sources
    Column("is_current", Boolean, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

insight_evidence = Table(
    "insight_evidence",
    metadata,
    Column("evidence_id", Integer, primary_key=True, autoincrement=True),
    Column("insight_id", Integer, ForeignKey("insight.insight_id"), nullable=False, index=True),
    Column("source_kind", Unicode(12), nullable=False),  # DOCUMENT | METRIC | SIGNAL | NEWS
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("section_code", Unicode(20)),
    Column("metric_code", Unicode(60)),
    Column("signal_id", Integer),
    Column("activity_id", Integer),
    Column("snippet", Unicode(1000), nullable=False),
    # Readable title and link of the source (Phase 8, enrich/sources.py)
    Column("source_title", Unicode(500)),
    Column("source_url", Unicode(2000)),
    Column("source_date", Unicode(10)),
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
    Column("metric_code", Unicode(60), primary_key=True),
    # INSIGHT (chosen with the insight) | METRIC_DETAIL (chosen by the metricdetail stage)
    Column("link_source", Unicode(16), nullable=False, server_default="INSIGHT"),
)

followup_question = Table(
    "followup_question",
    metadata,
    Column("question_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("ordinal", Integer, nullable=False),
    Column("category", Unicode(20), nullable=False),
    Column("question", UnicodeText, nullable=False),
    Column("rationale", UnicodeText, nullable=False),
    # Phase 8 (followups_v2): background facts, the risk area probed, the D&O angle
    Column("summary", UnicodeText),
    Column("following_risk", Unicode(200)),
    Column("uw_relevance", UnicodeText),
    Column("system_priority", Unicode(10), nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
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
    Column("metric_code", Unicode(60), primary_key=True),
    Column("link_source", Unicode(16), nullable=False, server_default="QUESTION"),
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
    Column("source_kind", Unicode(12), nullable=False),  # DOCUMENT | METRIC | SIGNAL | NEWS
    Column("title", Unicode(500), nullable=False),
    Column("url", Unicode(2000)),
    Column("source_date", Unicode(10)),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("metric_code", Unicode(60)),
)

# Metric detail modal (Phase 8, decisions D-036) -----------------------------------------------

# Rule-generated methodology per (company, metric) with data.
metric_detail = Table(
    "metric_detail",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("metric_code", Unicode(60), primary_key=True),
    Column("methodology", UnicodeText, nullable=False),
    Column("latest_period", Unicode(20)),
    Column("latest_value_text", Unicode(40)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Which filing the latest quarter and latest fiscal year of a metric come from.
metric_source = Table(
    "metric_source",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("metric_code", Unicode(60), primary_key=True),
    Column("period_type", Unicode(2), primary_key=True),  # Q | FY
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_quarter", Integer, nullable=False),
    Column("derivation", Unicode(20), nullable=False),
    Column("title", Unicode(500), nullable=False),
    Column("url", Unicode(2000)),
    Column("form_type", Unicode(20)),
    Column("filed_at", Unicode(10)),
    Column("accession_no", Unicode(25)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# LLM underwriting relevance per metric; each generation is a new current set per company.
metric_insight = Table(
    "metric_insight",
    metadata,
    Column("metric_insight_id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("metric_code", Unicode(60), nullable=False),
    Column("uw_relevance", UnicodeText, nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Orchestration (Phase 6, decisions D-034). One row per run; one task per (company, stage).
pipeline_run = Table(
    "pipeline_run",
    metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("job", Unicode(40), nullable=False),  # nightly | morning | news | manual
    Column("trigger", Unicode(20), nullable=False),  # cron | manual
    Column("status", Unicode(12), nullable=False),  # RUNNING | SUCCEEDED | PARTIAL | FAILED
    Column("stages", Unicode(500), nullable=False),  # JSON list
    Column("tickers", Unicode(2000), nullable=False),  # JSON list
    Column("starts", Integer, nullable=False),  # 1 + number of resumes
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("stats_json", UnicodeText),
)

pipeline_task = Table(
    "pipeline_task",
    metadata,
    Column("task_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("pipeline_run.run_id"), nullable=False, index=True),
    Column("ticker", Unicode(16), nullable=False),
    Column("stage", Unicode(20), nullable=False),
    Column("ordinal", Integer, nullable=False),  # execution order within the run
    # PENDING | RUNNING | SUCCEEDED | FAILED | SKIPPED
    Column("status", Unicode(10), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("detail", Unicode(2000)),
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
    Column("accession_no", Unicode(25), nullable=False),
    Column("metric_code", Unicode(60), nullable=False),
    Column("period_type", Unicode(2), nullable=False),
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
    Column("task", Unicode(40), nullable=False),
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
    Column("rule_id", Unicode(40), nullable=False),
    Column("severity", Unicode(8), nullable=False),  # Error | Warning | Info
    Column("metric_code", Unicode(60)),
    Column("period", Unicode(20)),
    Column("message", Unicode(1000), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Risk scores (Phase 9.4, decisions D-037). Scales: 0.00-1.00, tone -1.00..1.00.

# One row per earnings call transcript (source_document.doc_id): chunk scores averaged by length.
transcript_tone = Table(
    "transcript_tone",
    metadata,
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), primary_key=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("overall_tone", Float),  # -1 (negative) .. 1 (positive)
    Column("confidence", Float),
    Column("hedging", Float),
    Column("uncertainty", Float),
    Column("forward_looking", Float),
    Column("liability_cues", Float),
    Column("n_chunks", Integer, nullable=False),  # chunks with a usable score
    Column("n_chunks_attempted", Integer, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("run_id", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Risk statements in a transcript; `flag_text` is a verbatim quote found in the chunk.
transcript_flag = Table(
    "transcript_flag",
    metadata,
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), primary_key=True),
    Column("flag_index", Integer, primary_key=True),
    Column("category", Unicode(30), nullable=False),
    Column("flag_text", Unicode(1000), nullable=False),
    Column("chunk_index", Integer, nullable=False),
)

# One row per news activity (scored on its canonical article and summary).
news_risk_score = Table(
    "news_risk_score",
    metadata,
    Column("activity_id", Integer, ForeignKey("activity.activity_id"), primary_key=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("litigation", Float, nullable=False),
    Column("regulatory", Float, nullable=False),
    Column("financial_distress", Float, nullable=False),
    Column("governance", Float, nullable=False),
    Column("operational", Float, nullable=False),
    Column("overall", Float, nullable=False),
    Column("rationale", Unicode(1000), nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("run_id", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Board composition from the latest proxy statement (DEF 14A), one row per company (decisions
# D-045). Each fact holds only where both models read it the same way and its quote is found in
# the proxy; otherwise it is NULL. `evidence_json`: {fact: quote}.
board_composition = Table(
    "board_composition",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("board_size", Integer),
    Column("independent_directors", Integer),
    Column("ceo_is_chair", Boolean),
    Column("lead_independent_director", Unicode(200)),  # name; NULL when there is none
    Column("committees_json", UnicodeText, nullable=False),  # [{"name", "chair"}]
    # Phase 15f (board_v2, D-050): the proxy's auditor fee table for its latest fiscal year (USD),
    # more than one class of common stock with unequal votes, and the CEO's start year.
    Column("fees_fiscal_year", Integer),
    Column("audit_fees", Float(53)),
    Column("audit_related_fees", Float(53)),
    Column("tax_fees", Float(53)),
    Column("other_fees", Float(53)),
    Column("dual_class", Boolean),
    Column("ceo_name", Unicode(200)),
    Column("ceo_since", Integer),
    Column("evidence_json", UnicodeText, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Litigation and regulatory matters (Phase 15e, D-050), read by two models from the latest 10-K
# and the 10-Qs after it; a matter is kept only where both models found it with the same type
# and a quote found in the filing. Matched across filings by type and name; a matter first seen
# after a company's first filing is NEW and goes to `uw-pipeline review`.
legal_matter = Table(
    "legal_matter",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("matter_key", Unicode(160), primary_key=True),  # type:name tokens
    Column("matter_type", Unicode(30), nullable=False),
    Column("name", Unicode(300), nullable=False),
    Column("agency", Unicode(100)),
    Column("status", Unicode(20), nullable=False),  # open | settled | dismissed | closed | unknown
    Column("started", Unicode(30)),  # as the filing gives it ("March 2024", "2024")
    Column("wells_notice", Boolean, nullable=False),
    Column("accrual", Float(53)),  # USD
    Column("quote", UnicodeText, nullable=False),  # from the latest filing that mentions it
    Column("first_doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("last_doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("first_disclosed", Unicode(10), nullable=False),  # filed date of first_doc
    Column("last_seen", Unicode(10), nullable=False),  # filed date of last_doc
    Column("review_status", Unicode(20), nullable=False),  # BASELINE | NEW
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Clinical-stage pipeline (Phase 17c, D-058): the programs of a company with the clinical_stage
# trait, from its latest 10-K and later 10-Qs, both models agreeing. One row per candidate and
# indication; a program no longer in the latest filings is kept with is_current = False.
clinical_program = Table(
    "clinical_program",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("program_key", Unicode(200), primary_key=True),  # name words:indication words
    Column("name", Unicode(200), nullable=False),
    Column("indication", Unicode(300), nullable=False),
    Column("phase", Unicode(20), nullable=False),  # preclinical ... phase_3 | submitted | approved
    Column("next_milestone", Unicode(300)),
    Column("milestone_timing", Unicode(100)),  # as written ("second half of 2026")
    Column("quote", UnicodeText, nullable=False),
    Column("milestone_quote", UnicodeText),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Industry group chosen by the LLM for a company whose SIC code maps to no group (Phase 17g,
# D-059). AGREED: both models chose `group_code`; DISPUTED: they differed, the tie-breaker's
# choice is used and waits in `uw-pipeline review` (a rejected one is not used).
industry_choice = Table(
    "industry_choice",
    metadata,
    Column("company_id", Integer, ForeignKey("company.company_id"), primary_key=True),
    Column("group_code", Unicode(40), nullable=False),
    Column("status", Unicode(10), nullable=False),  # AGREED | DISPUTED
    Column("extractor_group", Unicode(40)),
    Column("tiebreaker_group", Unicode(40)),
    Column("quote", UnicodeText, nullable=False),
    Column("reason", Unicode(1000), nullable=False),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id"), nullable=False),
    Column("model", Unicode(60), nullable=False),
    Column("prompt_version", Unicode(40), nullable=False),
    Column("input_hash", Unicode(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Human decisions on items the pipeline could not settle (`uw-pipeline review`, decisions D-045):
# DISPUTED / OVERRULED signals and identity breaks. Only a named reviewer decides; a rejected
# signal is left out of insights and the review queue.
review_decision = Table(
    "review_decision",
    metadata,
    Column("item_type", Unicode(20), primary_key=True),  # SIGNAL | IDENTITY | MATTER
    Column("item_key", Unicode(100), primary_key=True),  # signal_key, or dq rule + period
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False),
    Column("decision", Unicode(10), nullable=False),  # APPROVED | REJECTED
    Column("reviewer", Unicode(100), nullable=False),
    Column("note", Unicode(1000)),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)

# -- Market data (Phase 15a, decisions D-050) ---------------------------------------------------

# Daily prices by symbol (the companies' tickers and the benchmark, SPY). `adjusted_close` is
# split- and dividend-adjusted (returns, drops, volatility); `close` is as traded (market cap).
market_daily = Table(
    "market_daily",
    metadata,
    Column("symbol", Unicode(16), primary_key=True),
    Column("trade_date", Unicode(10), primary_key=True),
    Column("close", Float, nullable=False),
    Column("adjusted_close", Float, nullable=False),
    Column("volume", Float),
    Column("raw_path", Unicode(500), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Quarterly earnings per share against the consensus estimate (Alpha Vantage EARNINGS).
earnings_report = Table(
    "earnings_report",
    metadata,
    Column("symbol", Unicode(16), primary_key=True),
    Column("fiscal_date_ending", Unicode(10), primary_key=True),
    Column("reported_date", Unicode(10)),
    Column("report_time", Unicode(20)),  # pre-market | post-market
    Column("reported_eps", Float),
    Column("estimated_eps", Float),
    Column("surprise", Float),
    Column("surprise_pct", Float),  # percent, as Alpha Vantage gives it
    Column("raw_path", Unicode(500), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Insiders' Form 4 transactions (Phase 15b, D-050): non-derivative lines of each filing.
insider_transaction = Table(
    "insider_transaction",
    metadata,
    Column("accession_no", Unicode(25), primary_key=True),
    Column("line", Integer, primary_key=True),
    Column("company_id", Integer, ForeignKey("company.company_id"), nullable=False, index=True),
    Column("doc_id", Integer, ForeignKey("source_document.doc_id")),
    Column("filed_at", Unicode(10), nullable=False),
    Column("owner_cik", Unicode(10)),
    Column("owner_name", Unicode(200)),
    Column("is_director", Boolean, nullable=False),
    Column("is_officer", Boolean, nullable=False),
    Column("officer_title", Unicode(200)),
    Column("is_ten_percent", Boolean, nullable=False),
    Column("security_title", Unicode(200)),
    Column("trade_date", Unicode(10)),
    Column("code", Unicode(4)),  # S sale, P purchase, M, F, A, G, ...
    Column("acquired_disposed", Unicode(1)),
    Column("shares", Float),
    Column("price", Float),
    Column("shares_after", Float),
    Column("direct", Boolean, nullable=False),
    Column("is_10b5_1", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
