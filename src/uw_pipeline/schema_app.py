"""Tables of the Professional 360 application database that the `publish` stage writes
(Phase 9, decisions D-037). They follow the team's data model diagram, plus the agreed changes:
ItemLink and CompanyMetricDetail (new), LoanPortfolio/LoanTypes/FollowUpQuestions/KeyInsights
columns. User tables (Users, UserHistory, ...) and views are owned by the application and not
defined here; the pipeline never writes them.

Strings are Unicode (NVARCHAR on SQL Server; the text has curly quotes and non-ASCII names).
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
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Unicode,
    UnicodeText,
)

app_metadata = MetaData()


def _id(name: str) -> Column[int]:
    return Column(name, Integer, primary_key=True, autoincrement=True)


SIC_Code = Table(
    "SIC_Code", app_metadata,
    _id("SIC_Id"),
    Column("SIC_Code", Unicode(4), nullable=False),
    Column("Description", Unicode(200)),
)  # fmt: skip

Company = Table(
    "Company", app_metadata,
    _id("CompanyId"),
    Column("SIC_Id", Integer, ForeignKey("SIC_Code.SIC_Id")),
    Column("CompanyName", Unicode(200), nullable=False),
    Column("NameAbbreviation", Unicode(16)),
    Column("Location", Unicode(200)),
    Column("logo", Unicode(500)),  # owned by the application; never written by the pipeline
    Column("UpdatedAt", Date),
    Column("CIK", Unicode(10), nullable=False),
)  # fmt: skip

metric_category = Table(
    "metric_category", app_metadata,
    _id("metric_category_Id"),
    Column("categoryName", Unicode(60), nullable=False),
)  # fmt: skip

metrics = Table(
    "metrics", app_metadata,
    _id("metric_id"),
    Column("metric_name", Unicode(120), nullable=False),
    Column("metric_category_Id", Integer, ForeignKey("metric_category.metric_category_Id")),
    Column("subtitle", Unicode(200)),
    Column("description", Unicode(1000)),
    Column("Formula", Unicode(500)),
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
    Column("cik", Unicode(10)),
    Column("value", Float(53)),
    Column("unit", Unicode(20)),
    Column("status", Unicode(10)),  # OK | NM
    # REPORTED | DERIVED | COMPUTED | LLM_EXTRACTED | PRELIMINARY
    Column("source_class", Unicode(20)),
    Column("formula", Unicode(300)),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("fiscal_year", Integer),
    Column("fiscal_period", Unicode(4)),  # Q1-Q4 | FY
    Column("accession", Unicode(500)),  # JSON list
    Column("form", Unicode(20)),
    Column("filed_date", Unicode(200)),  # JSON list of dates
    Column("concept_or_section", Unicode(1000)),
    Column("context_or_dimension", Unicode(1000)),  # JSON list
    Column("confidence", Numeric(5, 2)),
    Column("notes", Unicode(1000)),
)  # fmt: skip

KeyInsightsTypes = Table(
    "KeyInsightsTypes", app_metadata,
    _id("KeyInsightsType_Id"),
    Column("TypeName", Unicode(20), nullable=False),
)  # fmt: skip

KeyInsights = Table(
    "KeyInsights", app_metadata,
    _id("UniqueID"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("title", Unicode(300), nullable=False),
    Column("summary", UnicodeText),
    Column("KeyInsightsType_Id", Integer, ForeignKey("KeyInsightsTypes.KeyInsightsType_Id")),
    Column("iconType", Unicode(30)),
    Column("evidence", UnicodeText),  # Supporting Evidence ("source: “quote”" lines)
    Column("underwriting", UnicodeText),
    Column("source", UnicodeText),  # JSON [{title, url, date}]
    Column("updatedDate", Date),
    Column("link", Unicode(2000)),  # first source url
    Column("confidence", Numeric(5, 2)),
    Column("isActive", Boolean, nullable=False),  # Phase 9 addition
)  # fmt: skip

RiskTypes = Table(
    "RiskTypes", app_metadata,
    _id("RiskType_Id"),
    Column("RiskTypeName", Unicode(100), nullable=False),
)  # fmt: skip

StrategicRisk = Table(
    "StrategicRisk", app_metadata,
    _id("StrategicRiskId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("Category", Unicode(20), nullable=False),  # governance | risk | event | behavior
    Column("RiskType_Id", Integer, ForeignKey("RiskTypes.RiskType_Id")),
    Column("UpdatedAt", Date),
    Column("Title", Unicode(300), nullable=False),
    Column("Summary", UnicodeText),
    Column("UnderwritingRelevance", UnicodeText),
    Column("SupportingEvidence", UnicodeText),
    Column("sources", UnicodeText),  # JSON [{title, url, date}]
    Column("isActive", Boolean, nullable=False),
)  # fmt: skip

Activities = Table(
    "Activities", app_metadata,
    _id("ActivityId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("title", Unicode(1000), nullable=False),
    Column("updateDate", DateTime),
    Column("summary", UnicodeText),
    Column("sentiment", Unicode(20)),
    Column("underwriting", UnicodeText),
    Column("sources", UnicodeText),  # JSON [{title, url, date}]
    Column("confidence", Numeric(5, 2)),
)  # fmt: skip

News = Table(
    "News", app_metadata,
    _id("NewsId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("ArticleId", Integer),  # pipeline news_item id (alpha_NewsArticle is replaced)
    Column("NewsTitle", Unicode(1000), nullable=False),
    Column("Description", UnicodeText),
    Column("NewsReleaseDateTime", DateTime),
)  # fmt: skip

LoanTypes = Table(
    "LoanTypes", app_metadata,
    _id("LoanTypeId"),
    Column("LoanTypeName", Unicode(200), nullable=False),
    Column("ParentSegment", Unicode(200)),  # Phase 9 addition
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
    Column("TrendName", Unicode(120), nullable=False),
    Column("Methodology", UnicodeText),
)  # fmt: skip

Trend = Table(
    "Trend", app_metadata,
    _id("TrendId"),
    Column("TrendTypeId", Integer, ForeignKey("TrendTypes.TrendTypeId"), nullable=False),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("Year", Integer, nullable=False),
    Column("Value", Float(53)),
    Column("DataSources", UnicodeText),  # JSON [{title, url, date}]
    Column("DataPoints", UnicodeText),  # JSON [{label, text}]
)  # fmt: skip

FollowUpQuestions = Table(
    "FollowUpQuestions", app_metadata,
    _id("FollowUpQuestionId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("category", Unicode(20)),
    Column("title", UnicodeText, nullable=False),  # the question
    Column("summary", UnicodeText),
    Column("followingRisk", Unicode(200)),
    Column("underwritingRelevance", UnicodeText),
    Column("sources", UnicodeText),  # JSON [{title, url, date}]
    Column("Priority", Unicode(10)),  # Phase 9 addition: high | medium | low
    Column("isActive", Boolean, nullable=False),  # Phase 9 addition
)  # fmt: skip

# Phase 9 additions -------------------------------------------------------------------------

# Every "Related ..." tab. Types: KEY_INSIGHT | STRATEGIC_RISK | FOLLOWUP | METRIC (metric_id).
# Read both ways: links to a metric are found by (ToType = 'METRIC', ToId = metric_id).
ItemLink = Table(
    "ItemLink", app_metadata,
    _id("ItemLinkId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("FromType", Unicode(20), nullable=False),
    Column("FromId", Integer, nullable=False),
    Column("ToType", Unicode(20), nullable=False),
    Column("ToId", Integer, nullable=False),
    Column("LinkSource", Unicode(16)),  # INSIGHT | QUESTION | METRIC_DETAIL
)  # fmt: skip

# Left side of the metric modal (and of the Loan Portfolio modal, as metric `loan_portfolio`).
CompanyMetricDetail = Table(
    "CompanyMetricDetail", app_metadata,
    _id("CompanyMetricDetailId"),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("metric_id", Integer, ForeignKey("metrics.metric_id"), nullable=False),
    Column("Methodology", UnicodeText),
    Column("DataSources", UnicodeText),  # JSON [{title, url, date}]
    Column("DataPoints", UnicodeText),  # JSON [{label, text}]
    Column("UnderwritingRelevance", UnicodeText),
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
    Column("Quarter", Unicode(10)),  # e.g. 2026Q2
    Column("overallTone", Numeric(5, 2)),  # -1.00 .. 1.00
    Column("confidence", Numeric(5, 2)),
    Column("hedging", Numeric(5, 2)),
    Column("uncertainty", Numeric(5, 2)),
    Column("forwardLooking", Numeric(5, 2)),
    Column("liabilityCues", Numeric(5, 2)),
    Column("nChunks", Integer),
    Column("NChunksAttempted", Integer),
    Column("modelVersion", Unicode(100)),
    Column("createdAt", DateTime),
)  # fmt: skip

TranscriptRiskFlag = Table(
    "TranscriptRiskFlag", app_metadata,
    Column("TranscriptsId", Integer, primary_key=True),
    Column("runId", Integer, primary_key=True),
    Column("FlagIndex", Integer, primary_key=True),
    Column("FlagText", Unicode(1000), nullable=False),
    Column("Category", Unicode(30)),  # Phase 9 addition
)  # fmt: skip

NewsRiskScore = Table(
    "NewsRiskScore", app_metadata,
    Column("NewsId", Integer, ForeignKey("News.NewsId"), primary_key=True),
    Column("runId", Integer, primary_key=True),
    Column("CompanyId", Integer, ForeignKey("Company.CompanyId"), nullable=False),
    Column("ActivityId", Integer),  # Phase 9 addition: the event scored (app Activities.ActivityId)
    Column("litigationEO", Numeric(5, 2)),
    Column("regulatory", Numeric(5, 2)),
    Column("financialDistress", Numeric(5, 2)),
    Column("governance", Numeric(5, 2)),
    Column("operational", Numeric(5, 2)),
    Column("overallRiskScore", Numeric(5, 2)),
    Column("rationale", Unicode(1000)),
    Column("modelVersion", Unicode(100)),
    Column("createdAt", DateTime),
)  # fmt: skip

# Pipeline bookkeeping in the app database ---------------------------------------------------

pipeline_publish_map = Table(
    "pipeline_publish_map", app_metadata,
    Column("entity", Unicode(40), primary_key=True),  # app table name
    Column("pipeline_key", Unicode(200), primary_key=True),
    Column("app_id", Integer, nullable=False),
    Column("row_hash", Unicode(64), nullable=False),  # unchanged rows are not rewritten
    Column("published_at", DateTime(timezone=True), nullable=False),
)  # fmt: skip


# Indexes for the BFF's reads (current rows of a company; links from either end).
Index("ix_KeyInsights_company_active", KeyInsights.c.CompanyId, KeyInsights.c.isActive)
Index("ix_FollowUpQuestions_company_active", FollowUpQuestions.c.CompanyId,
      FollowUpQuestions.c.isActive)  # fmt: skip
Index("ix_LoanPortfolio_company_period", LoanPortfolio.c.CompanyId, LoanPortfolio.c.PeriodEnd)
Index("ix_ItemLink_from", ItemLink.c.CompanyId, ItemLink.c.FromType, ItemLink.c.FromId)
Index("ix_ItemLink_to", ItemLink.c.CompanyId, ItemLink.c.ToType, ItemLink.c.ToId)
Index("ix_CompanyMetricDetail_company_metric", CompanyMetricDetail.c.CompanyId,
      CompanyMetricDetail.c.metric_id)  # fmt: skip
