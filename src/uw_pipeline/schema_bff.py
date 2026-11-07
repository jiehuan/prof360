"""Tables the BFF owns in the application database that the pipeline only reads (Phase 19.C,
D-092): the metric chat's turns and the users' reports. They are created by
deploy/sql/bff_metric_chat.sql (a test keeps these definitions equal to it), never by the
pipeline: `bff_metadata` is not passed to create_all or the DDL export, except in tests.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Unicode,
    UnicodeText,
    Uuid,
)

bff_metadata = MetaData()

UserMetricChat = Table(
    "UserMetricChat", bff_metadata,
    # BIGINT on SQL Server; INTEGER on SQLite (local tests), the only type it auto-increments.
    Column("TurnId", BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
           autoincrement=True),
    Column("ConversationId", Uuid, nullable=False),
    Column("UserId", Integer, nullable=False),
    Column("CompanyId", Integer, nullable=False),
    Column("metric_id", Integer, nullable=False),
    Column("Question", Unicode(2000), nullable=False),
    Column("Answer", UnicodeText),
    Column("RawAnswer", UnicodeText),
    Column("Citations", UnicodeText),
    Column("PossibleIssue", Boolean, nullable=False, default=False),
    Column("IssueSummary", Unicode(1000)),
    Column("Grounding", Unicode(20), nullable=False),
    Column("GroundingDetail", Unicode(1000)),
    Column("Model", Unicode(60)),
    Column("PromptVersion", Unicode(40)),
    Column("ContextVersion", Integer),
    Column("ContextUpdatedAt", DateTime),
    Column("InputTokens", Integer),
    Column("OutputTokens", Integer),
    Column("LatencyMs", Integer),
    Column("CreatedAt", DateTime, nullable=False),
)  # fmt: skip

MetricIssueReport = Table(
    "MetricIssueReport", bff_metadata,
    Column("ReportId", Integer, primary_key=True, autoincrement=True),
    Column("UserId", Integer, nullable=False),
    Column("CompanyId", Integer, nullable=False),
    Column("metric_id", Integer, nullable=False),
    Column("TurnId", BigInteger),
    Column("Source", Unicode(10), nullable=False),  # USER | MODEL
    Column("Period", Unicode(10)),
    Column("Comment", Unicode(2000)),
    Column("IssueSummary", Unicode(1000)),
    Column("CreatedAt", DateTime, nullable=False),
)  # fmt: skip
