"""Tables of the Professional 360 application database that the `publish` stage writes
(Phase 9, decisions D-037). They follow the team's data model diagram, plus the agreed changes:
ItemLink and CompanyMetricDetail (new), LoanPortfolio/LoanTypes/FollowUpQuestions/KeyInsights
columns. User tables (Users, UserHistory, ...) and views are owned by the application and not
defined here; the pipeline never writes them.

Ids are identity columns owned by the application database. `pipeline_publish_map` (in the same
database, so a row and its mapping commit together) maps each pipeline object to its app id.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

app_metadata = MetaData()


def _id(name: str) -> Column[int]:
    return Column(name, Integer, primary_key=True, autoincrement=True)


SIC_Code = Table(
    "SIC_Code", app_metadata,
    _id("SIC_Id"),
    Column("SIC_Code", String(4), nullable=False),
    Column("Description", String(200)),
)  # fmt: skip

Company = Table(
    "Company", app_metadata,
    _id("CompanyId"),
    Column("SIC_Id", Integer, ForeignKey("SIC_Code.SIC_Id")),
    Column("CompanyName", String(200), nullable=False),
    Column("NameAbbreviation", String(16)),
    Column("Location", String(200)),
    Column("logo", String(500)),  # owned by the application; never written by the pipeline
    Column("UpdatedAt", Date),
    Column("CIK", String(10), nullable=False),
)  # fmt: skip

metric_category = Table(
    "metric_category", app_metadata,
    _id("metric_category_Id"),
    Column("categoryName", String(60), nullable=False),
)  # fmt: skip

metrics = Table(
    "metrics", app_metadata,
    _id("metric_id"),
    Column("metric_name", String(120), nullable=False),
    Column("metric_category_Id", Integer, ForeignKey("metric_category.metric_category_Id")),
    Column("subtitle", String(200)),
    Column("description", String(1000)),
    Column("Formula", String(500)),
)  # fmt: skip

SIC_Preferences = Table(
    "SIC_Preferences", app_metadata,
    _id("SIC_PreferencesId"),
    Column("metric_id", Integer, ForeignKey("metrics.metric_id"), nullable=False),
    Column("SIC_Id", Integer, ForeignKey("SIC_Code.SIC_Id"), nullable=False),
)  # fmt: skip

MatrixData = Table(
    "MatrixData", app_metadata,
    _id("MatrixDataId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("metric_id", Integer, ForeignKey("metrics.metric_id"), nullable=False),
    Column("cik", String(10)),
    Column("value", Float(53)),
    Column("unit", String(20)),
    Column("status", String(10)),  # OK | NM
    # REPORTED | DERIVED | COMPUTED | LLM_EXTRACTED | PRELIMINARY
    Column("source_class", String(20)),
    Column("formula", String(300)),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("fiscal_year", Integer),
    Column("fiscal_period", String(4)),  # Q1-Q4 | FY
    Column("accession", String(500)),  # JSON list
    Column("form", String(20)),
    Column("filed_date", String(200)),  # JSON list of dates
    Column("concept_or_section", String(1000)),
    Column("context_or_dimension", String(1000)),  # JSON list
    Column("confidence", Numeric(5, 2)),
    Column("notes", String(1000)),
)  # fmt: skip

KeyInsightsTypes = Table(
    "KeyInsightsTypes", app_metadata,
    _id("KeyInsightsType_Id"),
    Column("TypeName", String(20), nullable=False),
)  # fmt: skip

KeyInsights = Table(
    "KeyInsights", app_metadata,
    _id("UniqueID"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("title", String(300), nullable=False),
    Column("summary", Text),
    Column("KeyInsightsType_Id", Integer, ForeignKey("KeyInsightsTypes.KeyInsightsType_Id")),
    Column("iconType", String(30)),
    Column("evidence", Text),  # Supporting Evidence ("source: “quote”" lines)
    Column("underwriting", Text),
    Column("source", Text),  # JSON [{title, url, date}]
    Column("updatedDate", Date),
    Column("link", String(2000)),  # first source url
    Column("confidence", Numeric(5, 2)),
    Column("isActive", Boolean, nullable=False),  # Phase 9 addition
)  # fmt: skip

RiskTypes = Table(
    "RiskTypes", app_metadata,
    _id("RiskType_Id"),
    Column("RiskTypeName", String(100), nullable=False),
)  # fmt: skip

StrategicRisk = Table(
    "StrategicRisk", app_metadata,
    _id("StrategicRiskId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("Category", String(20), nullable=False),  # governance | risk | event | behavior
    Column("RiskType_Id", Integer, ForeignKey("RiskTypes.RiskType_Id")),
    Column("UpdatedAt", Date),
    Column("Title", String(300), nullable=False),
    Column("Summary", Text),
    Column("UnderwritingRelevance", Text),
    Column("SupportingEvidence", Text),
    Column("sources", Text),  # JSON [{title, url, date}]
    Column("isActive", Boolean, nullable=False),
)  # fmt: skip

Activities = Table(
    "Activities", app_metadata,
    _id("ActivityId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("title", String(1000), nullable=False),
    Column("updateDate", DateTime),
    Column("summary", Text),
    Column("sentiment", String(20)),
    Column("underwriting", Text),
    Column("sources", Text),  # JSON [{title, url, date}]
    Column("confidence", Numeric(5, 2)),
)  # fmt: skip

News = Table(
    "News", app_metadata,
    _id("NewsId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("ArticleId", Integer),  # pipeline news_item id (alpha_NewsArticle is replaced)
    Column("NewsTitle", String(1000), nullable=False),
    Column("Description", Text),
    Column("NewsReleaseDateTime", DateTime),
)  # fmt: skip

LoanTypes = Table(
    "LoanTypes", app_metadata,
    _id("LoanTypeId"),
    Column("LoanTypeName", String(200), nullable=False),
    Column("ParentSegment", String(200)),  # Phase 9 addition
)  # fmt: skip

LoanPortfolio = Table(
    "LoanPortfolio", app_metadata,
    _id("LoanPortfolioId"),
    Column("LoanTypeId", Integer, ForeignKey("LoanTypes.LoanTypeId"), nullable=False),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("LoanValue", Numeric(19, 2), nullable=False),
    Column("UpdatedAt", Date),
    # Phase 9 additions
    Column("PeriodEnd", Date, nullable=False),
    Column("PctOfBook", Numeric(7, 4)),
    Column("Nonaccrual", Numeric(19, 2)),
    Column("NonaccrualRatio", Numeric(7, 4)),
)  # fmt: skip

TrendTypes = Table(
    "TrendTypes", app_metadata,
    _id("TrendTypeId"),
    Column("TrendName", String(120), nullable=False),
    Column("Methodology", Text),
)  # fmt: skip

Trend = Table(
    "Trend", app_metadata,
    _id("TrendId"),
    Column("TrendTypeId", Integer, ForeignKey("TrendTypes.TrendTypeId"), nullable=False),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("Year", Integer, nullable=False),
    Column("Value", Float(53)),
    Column("DataSources", Text),  # JSON [{title, url, date}]
    Column("DataPoints", Text),  # JSON [{label, text}]
)  # fmt: skip

FollowUpQuestions = Table(
    "FollowUpQuestions", app_metadata,
    _id("FollowUpQuestionId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("category", String(20)),
    Column("title", Text, nullable=False),  # the question
    Column("summary", Text),
    Column("followingRisk", String(200)),
    Column("underwritingRelevance", Text),
    Column("sources", Text),  # JSON [{title, url, date}]
    Column("Priority", String(10)),  # Phase 9 addition: high | medium | low
    Column("isActive", Boolean, nullable=False),  # Phase 9 addition
)  # fmt: skip

# Phase 9 additions -------------------------------------------------------------------------

# Every "Related ..." tab. Types: KEY_INSIGHT | STRATEGIC_RISK | FOLLOWUP | METRIC (metric_id).
# Read both ways: links to a metric are found by (ToType = 'METRIC', ToId = metric_id).
ItemLink = Table(
    "ItemLink", app_metadata,
    _id("ItemLinkId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("FromType", String(20), nullable=False),
    Column("FromId", Integer, nullable=False),
    Column("ToType", String(20), nullable=False),
    Column("ToId", Integer, nullable=False),
    Column("LinkSource", String(16)),  # INSIGHT | QUESTION | METRIC_DETAIL
)  # fmt: skip

# Left side of the metric modal (and of the Loan Portfolio modal, as metric `loan_portfolio`).
CompanyMetricDetail = Table(
    "CompanyMetricDetail", app_metadata,
    _id("CompanyMetricDetailId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("metric_id", Integer, ForeignKey("metrics.metric_id"), nullable=False),
    Column("Methodology", Text),
    Column("DataSources", Text),  # JSON [{title, url, date}]
    Column("DataPoints", Text),  # JSON [{label, text}]
    Column("UnderwritingRelevance", Text),
    Column("UpdatedAt", DateTime),
)  # fmt: skip

# Risk scores (green tables in the diagram; Phase 9.4). TranscriptsId is the pipeline's
# transcript document id (alpha_Transcripts is replaced); runId is the pipeline run that scored.

TranscriptToneAnalysis = Table(
    "TranscriptToneAnalysis", app_metadata,
    _id("OneAnalysisId"),
    Column("TranscriptsId", Integer, nullable=False),
    Column("runId", Integer, nullable=False),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("Quarter", String(10)),  # e.g. 2026Q2
    Column("overallTone", Numeric(5, 2)),  # -1.00 .. 1.00
    Column("confidence", Numeric(5, 2)),
    Column("hedging", Numeric(5, 2)),
    Column("uncertainty", Numeric(5, 2)),
    Column("forwardLooking", Numeric(5, 2)),
    Column("liabilityCues", Numeric(5, 2)),
    Column("nChunks", Integer),
    Column("NChunksAttempted", Integer),
    Column("modelVersion", String(100)),
    Column("createdAt", DateTime),
)  # fmt: skip

TranscriptRiskFlag = Table(
    "TranscriptRiskFlag", app_metadata,
    Column("TranscriptsId", Integer, primary_key=True),
    Column("runId", Integer, primary_key=True),
    Column("FlagIndex", Integer, primary_key=True),
    Column("FlagText", String(1000), nullable=False),
    Column("Category", String(30)),  # Phase 9 addition
)  # fmt: skip

NewsRiskScore = Table(
    "NewsRiskScore", app_metadata,
    Column("NewsId", Integer, ForeignKey("News.NewsId"), primary_key=True),
    Column("runId", Integer, primary_key=True),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("ActivityId", Integer),  # Phase 9 addition: the de-duplicated event scored
    Column("litigationEO", Numeric(5, 2)),
    Column("regulatory", Numeric(5, 2)),
    Column("financialDistress", Numeric(5, 2)),
    Column("governance", Numeric(5, 2)),
    Column("operational", Numeric(5, 2)),
    Column("overallRiskScore", Numeric(5, 2)),
    Column("rationale", String(1000)),
    Column("modelVersion", String(100)),
    Column("createdAt", DateTime),
)  # fmt: skip

# Pipeline bookkeeping in the app database ---------------------------------------------------

pipeline_publish_map = Table(
    "pipeline_publish_map", app_metadata,
    Column("entity", String(40), primary_key=True),  # app table name
    Column("pipeline_key", String(200), primary_key=True),
    Column("app_id", Integer, nullable=False),
    Column("row_hash", String(64), nullable=False),  # unchanged rows are not rewritten
    Column("published_at", DateTime(timezone=True), nullable=False),
)  # fmt: skip
