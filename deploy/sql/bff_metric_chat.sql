-- Metric chat (Phase 19, D-088, D-090) for the BFF, to add on its own. Schema
-- [dbo_professional360]; if the tables are in [dbo], replace "dbo_professional360." with "dbo."
-- throughout. Safe to run more than once. Run in this order.
--
--   POST api/companies/{id}/metrics/{metricId}/chat
--        reads   udf_GetMetricChatContext(@CompanyId, @MetricId)   the metric's context
--                udf_GetActiveChatPrompt(N'metric_chat')           the prompt to use
--        writes  UserMetricChat                                    one row per question
--   POST api/companies/{id}/metrics/{metricId}/chat/{turnId}/report
--        writes  MetricIssueReport                                 "This looks wrong"
--
-- Who writes what: the pipeline's publish stage writes MetricChatContext (every run) and
-- ChatPrompt (from Phase 19.B); the BFF writes UserMetricChat and MetricIssueReport, which the
-- pipeline only reads (uw-pipeline review, Phase 19.C). Like the User* tables they are not in the
-- pipeline's copy (copy_sqlite_to_azure); sync_sql_objects copies them by name (D-075, D-086).

-- 1. Tables the pipeline writes (also in app_schema_changes.sql / app_schema_full.sql) ----------

-- MetricChatContext: per company and metric, the JSON the chat may use. Its shape is in
-- docs/schema-contract.md ("Metric chat context"); ContextVersion says which shape.
IF OBJECT_ID(N'dbo_professional360.MetricChatContext', N'U') IS NULL
CREATE TABLE dbo_professional360.[MetricChatContext] (
    [MetricChatContextId] INTEGER NOT NULL IDENTITY,
    [CompanyId] INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    [ContextVersion] INTEGER NOT NULL,
    [ContextJson] NVARCHAR(max) NOT NULL,
    [UpdatedAt] DATETIME NULL,
    PRIMARY KEY ([MetricChatContextId]),
    FOREIGN KEY([CompanyId]) REFERENCES dbo_professional360.[Company] ([CompanyId]),
    FOREIGN KEY(metric_id) REFERENCES dbo_professional360.metrics (metric_id)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_MetricChatContext_company_metric' AND object_id = OBJECT_ID(N'dbo_professional360.MetricChatContext'))
    CREATE INDEX [ix_MetricChatContext_company_metric] ON dbo_professional360.[MetricChatContext] ([CompanyId], metric_id);
GO

-- ChatPrompt: the chat's prompt versions. The BFF uses the one IsActive row of its task; the
-- uw_metric_chat package refuses a prompt whose MinPackageVersion is newer than itself.
IF OBJECT_ID(N'dbo_professional360.ChatPrompt', N'U') IS NULL
CREATE TABLE dbo_professional360.[ChatPrompt] (
    [ChatPromptId] INTEGER NOT NULL IDENTITY,
    [Task] NVARCHAR(40) NOT NULL,
    [PromptVersion] NVARCHAR(40) NOT NULL,
    [SystemText] NVARCHAR(max) NOT NULL,
    [OutputSchema] NVARCHAR(max) NOT NULL,
    [ContextVersions] NVARCHAR(40) NOT NULL,
    [MinPackageVersion] NVARCHAR(20) NOT NULL,
    [IsActive] BIT NOT NULL,
    [UpdatedAt] DATETIME NULL,
    PRIMARY KEY ([ChatPromptId])
);
GO

-- 2. Tables the BFF writes ---------------------------------------------------------------------

-- UserMetricChat: every question and answer (D-088: all chats are recorded). ConversationId
-- groups the turns of one chat (the BFF makes it). Answer is what the user saw: the model's
-- answer, or "can't confirm" when the answer failed the grounding check (Grounding = rejected,
-- GroundingDetail says why; the model's text is then in RawAnswer). Citations: JSON list of
-- the data-point ids the answer cites. Retention: 365 days assumed, to be confirmed
-- (compliance); delete older rows by CreatedAt.
IF OBJECT_ID(N'dbo_professional360.UserMetricChat', N'U') IS NULL
CREATE TABLE dbo_professional360.UserMetricChat (
    TurnId BIGINT NOT NULL IDENTITY PRIMARY KEY,
    ConversationId UNIQUEIDENTIFIER NOT NULL,
    UserId INT NOT NULL,
    CompanyId INT NOT NULL,
    metric_id INT NOT NULL,
    Question NVARCHAR(2000) NOT NULL,
    Answer NVARCHAR(max) NULL,
    RawAnswer NVARCHAR(max) NULL,
    Citations NVARCHAR(max) NULL,
    PossibleIssue BIT NOT NULL CONSTRAINT DF_UserMetricChat_PossibleIssue DEFAULT 0,
    IssueSummary NVARCHAR(1000) NULL,
    Grounding NVARCHAR(20) NOT NULL,  -- ok | rejected | error
    GroundingDetail NVARCHAR(1000) NULL,
    Model NVARCHAR(60) NULL,
    PromptVersion NVARCHAR(40) NULL,
    ContextVersion INT NULL,
    ContextUpdatedAt DATETIME NULL,
    InputTokens INT NULL,
    OutputTokens INT NULL,
    LatencyMs INT NULL,
    CreatedAt DATETIME NOT NULL CONSTRAINT DF_UserMetricChat_CreatedAt DEFAULT GETUTCDATE()
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_UserMetricChat_conversation' AND object_id = OBJECT_ID(N'dbo_professional360.UserMetricChat'))
    CREATE INDEX ix_UserMetricChat_conversation ON dbo_professional360.UserMetricChat (ConversationId, TurnId);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_UserMetricChat_user' AND object_id = OBJECT_ID(N'dbo_professional360.UserMetricChat'))
    CREATE INDEX ix_UserMetricChat_user ON dbo_professional360.UserMetricChat (UserId, CreatedAt);
GO

-- MetricIssueReport: a value a user thinks is wrong. Source: USER (the "This looks wrong"
-- button; Comment is the user's text) or MODEL (an answer with PossibleIssue = 1; the BFF
-- copies its IssueSummary). Period: the period concerned when known (e.g. FY2025, 2026Q3, as
-- in the context's data-point ids). The pipeline reads new reports into its review queue; its
-- decisions are kept on the pipeline side, not written back here.
IF OBJECT_ID(N'dbo_professional360.MetricIssueReport', N'U') IS NULL
CREATE TABLE dbo_professional360.MetricIssueReport (
    ReportId INT NOT NULL IDENTITY PRIMARY KEY,
    UserId INT NOT NULL,
    CompanyId INT NOT NULL,
    metric_id INT NOT NULL,
    TurnId BIGINT NULL,
    Source NVARCHAR(10) NOT NULL,  -- USER | MODEL
    Period NVARCHAR(10) NULL,
    Comment NVARCHAR(2000) NULL,
    IssueSummary NVARCHAR(1000) NULL,
    CreatedAt DATETIME NOT NULL CONSTRAINT DF_MetricIssueReport_CreatedAt DEFAULT GETUTCDATE()
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_MetricIssueReport_company_metric' AND object_id = OBJECT_ID(N'dbo_professional360.MetricIssueReport'))
    CREATE INDEX ix_MetricIssueReport_company_metric ON dbo_professional360.MetricIssueReport (CompanyId, metric_id, CreatedAt);
GO

-- 3. Read functions (also in bff_functions.sql) ------------------------------------------------

-- The chat context of one company's metric: one row, or none when the metric has no context
-- (not shown for this company, or no data). context is the JSON object itself.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetMetricChatContext (@CompanyId INT, @MetricId INT)
RETURNS TABLE
AS RETURN (
    SELECT TOP 1
        C.CompanyId AS companyId,
        C.metric_id AS metricId,
        C.ContextVersion AS contextVersion,
        JSON_QUERY(C.ContextJson) AS context,
        C.UpdatedAt AS updatedAt
    FROM dbo_professional360.MetricChatContext C
    WHERE C.CompanyId = @CompanyId AND C.metric_id = @MetricId
    ORDER BY C.UpdatedAt DESC
);
GO

-- The prompt the chat uses for @Task (N'metric_chat'): the active row, newest first.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetActiveChatPrompt (@Task NVARCHAR(40))
RETURNS TABLE
AS RETURN (
    SELECT TOP 1
        P.ChatPromptId AS promptId,
        P.PromptVersion AS promptVersion,
        P.SystemText AS systemText,
        JSON_QUERY(P.OutputSchema) AS outputSchema,
        P.ContextVersions AS contextVersions,
        P.MinPackageVersion AS minPackageVersion,
        P.UpdatedAt AS updatedAt
    FROM dbo_professional360.ChatPrompt P
    WHERE P.Task = @Task AND P.IsActive = 1
    ORDER BY P.UpdatedAt DESC, P.ChatPromptId DESC
);
GO
