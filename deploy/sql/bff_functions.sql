-- Read functions for the BFF over the application tables the pipeline publishes, one per
-- function file in professional-databricks/data_io/azure_sql/sql_queries/functions (same names,
-- spelling kept). A proposal from the pipeline side (2026-10-23); the BFF team owns the final
-- version. Field names follow the front-end API list; open points: docs/frontend-api-review.md.
--
-- Assumptions (check before use):
--   * Schema: application tables in [dbo_professional360] (config/azure.yaml, app_schema_full.sql).
--     If the tables are in [dbo], replace "dbo_professional360." with "dbo." throughout.
--   * User tables (owned by the BFF, not written by the pipeline), as in the team data model:
--     UserFavority (UserId, CompanyId, CreatedAt), UserPinnedCompanies (UserId, CompanyId,
--     PinnedAt), UserHistory (UserHistoryId, UserId, CompanyId, ViewedAt),
--     UserFlaggedFollowupQuestions (UserId, FollowUpQuestionId, FlaggedAt), and
--     UserMetricsSelection (UserId, CompanyId, metric_id, Selected) as in the BFF's
--     udf_GetUserMetricSelectionJson (per company; see review item C2).
--   * Versioned tables (KeyInsights, KeyTakeaways, StrategicRisk, FollowUpQuestions): only
--     isActive = 1 rows are current. Sources columns hold JSON [{title, url, date}].
--   * Azure SQL Database or SQL Server 2017+ (FOR JSON, JSON_VALUE, JSON_QUERY, STRING_AGG).
--     JSON is built with FOR JSON PATH, not JSON_OBJECT / JSON_ARRAYAGG (SQL Server 2022 / 2025).
--   * Writes (pin, favorite, view history, flag, metric selection) are stored procedures, not
--     functions: a function cannot write. Recording a view belongs in the "get company" call.
--   * Inline table-valued functions return no ORDER BY; each one names the column to order by.
--
-- Tested 2026-10-23 on SQL Server 2022: app_schema_full.sql, the user tables above and three
-- companies from data/app.db (Marriott, Pfizer, JPMorgan); every function created and called,
-- every JSON column valid. FLOAT values come out of FOR JSON in exponent form
-- (1.7702e+010), which is valid JSON.

-- == Helpers ========================================================================================

-- Related items of one item (modal Related tabs): ItemLink read from either end, active items only.
-- Returns {"keyInsights":[ids],"risks":[ids],"followUps":[ids],"metrics":[ids]}.
-- @ItemType: KEY_INSIGHT | STRATEGIC_RISK | FOLLOWUP | METRIC.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetItemRelationsJson (
    @CompanyId INT, @ItemType NVARCHAR(20), @ItemId INT
)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @L TABLE (ItemType NVARCHAR(20) NOT NULL, ItemId INT NOT NULL,
                      PRIMARY KEY (ItemType, ItemId));
    INSERT INTO @L (ItemType, ItemId)
    SELECT ToType, ToId FROM dbo_professional360.ItemLink
    WHERE CompanyId = @CompanyId AND FromType = @ItemType AND FromId = @ItemId
    UNION
    SELECT FromType, FromId FROM dbo_professional360.ItemLink
    WHERE CompanyId = @CompanyId AND ToType = @ItemType AND ToId = @ItemId;

    RETURN (
        SELECT
            JSON_QUERY(CONCAT('[', (
                SELECT STRING_AGG(CAST(L.ItemId AS NVARCHAR(12)), ',') WITHIN GROUP (ORDER BY L.ItemId)
                FROM @L L JOIN dbo_professional360.KeyInsights K
                  ON K.UniqueID = L.ItemId AND K.isActive = 1
                WHERE L.ItemType = N'KEY_INSIGHT'), ']')) AS keyInsights,
            JSON_QUERY(CONCAT('[', (
                SELECT STRING_AGG(CAST(L.ItemId AS NVARCHAR(12)), ',') WITHIN GROUP (ORDER BY L.ItemId)
                FROM @L L JOIN dbo_professional360.StrategicRisk R
                  ON R.StrategicRiskId = L.ItemId AND R.isActive = 1
                WHERE L.ItemType = N'STRATEGIC_RISK'), ']')) AS risks,
            JSON_QUERY(CONCAT('[', (
                SELECT STRING_AGG(CAST(L.ItemId AS NVARCHAR(12)), ',') WITHIN GROUP (ORDER BY L.ItemId)
                FROM @L L JOIN dbo_professional360.FollowUpQuestions Q
                  ON Q.FollowUpQuestionId = L.ItemId AND Q.isActive = 1
                WHERE L.ItemType = N'FOLLOWUP'), ']')) AS followUps,
            JSON_QUERY(CONCAT('[', (
                SELECT STRING_AGG(CAST(L.ItemId AS NVARCHAR(12)), ',') WITHIN GROUP (ORDER BY L.ItemId)
                FROM @L L
                WHERE L.ItemType = N'METRIC'
                  AND EXISTS (SELECT 1 FROM dbo_professional360.MatrixData D
                              WHERE D.CompanyId = @CompanyId AND D.metric_id = L.ItemId)), ']')) AS metrics
        FOR JSON PATH, WITHOUT_ARRAY_WRAPPER
    );
END;
GO

-- One metric as a MetricSelection: {"id":..,"title":..,"subtitle":..}.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetMetricJson (@MetricId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    RETURN (
        SELECT M.metric_id AS id, M.metric_name AS title, M.subtitle
        FROM dbo_professional360.metrics M
        WHERE M.metric_id = @MetricId
        FOR JSON PATH, WITHOUT_ARRAY_WRAPPER
    );
END;
GO

-- == Index page =====================================================================================

-- Risk flags (current Key Insights of type Warning or Negative) and current follow-up questions.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyRiskAndQuectionCount (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        (SELECT COUNT(*)
         FROM dbo_professional360.KeyInsights K
         JOIN dbo_professional360.KeyInsightsTypes KT ON KT.KeyInsightsType_Id = K.KeyInsightsType_Id
         WHERE K.CompanyId = @CompanyId AND K.isActive = 1
           AND KT.TypeName IN (N'Warning', N'Negative')) AS riskFlags,
        (SELECT COUNT(*)
         FROM dbo_professional360.FollowUpQuestions Q
         WHERE Q.CompanyId = @CompanyId AND Q.isActive = 1) AS followUpQuestions
);
GO

CREATE OR ALTER FUNCTION dbo_professional360.udf_IsFavorityCompanyOfUser (@UserId INT, @CompanyId INT)
RETURNS BIT
AS BEGIN
    RETURN CASE WHEN EXISTS (
        SELECT 1 FROM dbo_professional360.UserFavority
        WHERE UserId = @UserId AND CompanyId = @CompanyId) THEN 1 ELSE 0 END;
END;
GO

CREATE OR ALTER FUNCTION dbo_professional360.udf_IsCompanyPinnedForUser (@UserId INT, @CompanyId INT)
RETURNS BIT
AS BEGIN
    RETURN CASE WHEN EXISTS (
        SELECT 1 FROM dbo_professional360.UserPinnedCompanies
        WHERE UserId = @UserId AND CompanyId = @CompanyId) THEN 1 ELSE 0 END;
END;
GO

-- GET api/companies?search= : CompanySearchResult. Matches name or ticker; @Search NULL = all.
-- Order by: name.
CREATE OR ALTER FUNCTION dbo_professional360.udf_searchcompany (@UserId INT, @Search NVARCHAR(200))
RETURNS TABLE
AS RETURN (
    SELECT
        C.CompanyId AS id,
        C.CompanyName AS name,
        C.NameAbbreviation AS ticker,
        S.Description AS industry,
        C.logo,
        CAST(CASE WHEN F.CompanyId IS NULL THEN 0 ELSE 1 END AS BIT) AS isFavorite
    FROM dbo_professional360.Company C
    LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
    LEFT JOIN dbo_professional360.UserFavority F ON F.CompanyId = C.CompanyId AND F.UserId = @UserId
    WHERE @Search IS NULL OR LTRIM(RTRIM(@Search)) = N''
       OR C.CompanyName LIKE N'%' + LTRIM(RTRIM(@Search)) + N'%'
       OR C.NameAbbreviation LIKE N'%' + LTRIM(RTRIM(@Search)) + N'%'
);
GO

-- GET api/companies/favorited : CompanyInfo. Order by: isPinned DESC, name.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetFavorityComaniesForUser (@UserId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        C.CompanyId AS id,
        C.CompanyName AS name,
        C.NameAbbreviation AS ticker,
        S.Description AS industry,
        C.logo,
        C.Location AS location,
        N.riskFlags,
        N.followUpQuestions,
        C.UpdatedAt AS updatedAt,
        CAST(1 AS BIT) AS isFavorite,
        CAST(CASE WHEN P.CompanyId IS NULL THEN 0 ELSE 1 END AS BIT) AS isPinned
    FROM dbo_professional360.UserFavority F
    JOIN dbo_professional360.Company C ON C.CompanyId = F.CompanyId
    LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
    LEFT JOIN dbo_professional360.UserPinnedCompanies P
      ON P.CompanyId = C.CompanyId AND P.UserId = @UserId
    CROSS APPLY dbo_professional360.udf_GetCompanyRiskAndQuectionCount(C.CompanyId) N
    WHERE F.UserId = @UserId
);
GO

-- GET api/companies/viewed : CompanyInfoViewed, one row per company (latest view).
-- Order by: lastViewedAt DESC.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetViewedComaniesForUser (@UserId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        C.CompanyId AS id,
        C.CompanyName AS name,
        C.NameAbbreviation AS ticker,
        S.Description AS industry,
        C.logo,
        C.Location AS location,
        N.riskFlags,
        N.followUpQuestions,
        C.UpdatedAt AS updatedAt,
        CAST(CASE WHEN F.CompanyId IS NULL THEN 0 ELSE 1 END AS BIT) AS isFavorite,
        CAST(CASE WHEN P.CompanyId IS NULL THEN 0 ELSE 1 END AS BIT) AS isPinned,
        H.lastViewedAt
    FROM (SELECT CompanyId, MAX(ViewedAt) AS lastViewedAt
          FROM dbo_professional360.UserHistory
          WHERE UserId = @UserId
          GROUP BY CompanyId) H
    JOIN dbo_professional360.Company C ON C.CompanyId = H.CompanyId
    LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
    LEFT JOIN dbo_professional360.UserFavority F ON F.CompanyId = C.CompanyId AND F.UserId = @UserId
    LEFT JOIN dbo_professional360.UserPinnedCompanies P
      ON P.CompanyId = C.CompanyId AND P.UserId = @UserId
    CROSS APPLY dbo_professional360.udf_GetCompanyRiskAndQuectionCount(C.CompanyId) N
);
GO

-- == Detail page ====================================================================================

-- metricSelections: the metrics this company has values for, by category (the choices offered).
-- MetricSelectionCategory[] JSON; the loan-portfolio pseudo metric is left out (own module).
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyMetricSelectionJson (@CompanyId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @Have TABLE (metric_id INT PRIMARY KEY);
    INSERT INTO @Have (metric_id)
    SELECT DISTINCT D.metric_id
    FROM dbo_professional360.MatrixData D
    JOIN dbo_professional360.metrics M ON M.metric_id = D.metric_id
    WHERE D.CompanyId = @CompanyId AND M.metric_name <> N'Loan Portfolio Composition';

    RETURN ISNULL((
        SELECT
            MC.metric_category_Id AS id,
            MC.categoryName AS name,
            JSON_QUERY((
                SELECT M.metric_id AS id, M.metric_name AS title, M.subtitle
                FROM dbo_professional360.metrics M
                JOIN @Have H ON H.metric_id = M.metric_id
                WHERE M.metric_category_Id = MC.metric_category_Id
                ORDER BY M.metric_id
                FOR JSON PATH)) AS selections
        FROM dbo_professional360.metric_category MC
        WHERE EXISTS (SELECT 1 FROM dbo_professional360.metrics M JOIN @Have H ON H.metric_id = M.metric_id
                      WHERE M.metric_category_Id = MC.metric_category_Id)
        ORDER BY MC.metric_category_Id
        FOR JSON PATH), N'[]');
END;
GO

-- userMetricSelections: the user's choice for this company; if the user never chose, the
-- industry default (SIC_Preferences). Rows with Selected = 0 mean the user removed a metric, so
-- a user who removed everything gets an empty list, not the default.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetUserMetricSelectionJson (@UserId INT, @CompanyId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @Chosen TABLE (metric_id INT PRIMARY KEY);
    IF EXISTS (SELECT 1 FROM dbo_professional360.UserMetricsSelection
               WHERE UserId = @UserId AND CompanyId = @CompanyId)
        INSERT INTO @Chosen (metric_id)
        SELECT DISTINCT metric_id FROM dbo_professional360.UserMetricsSelection
        WHERE UserId = @UserId AND CompanyId = @CompanyId AND Selected = 1;
    ELSE
        INSERT INTO @Chosen (metric_id)
        SELECT DISTINCT P.metric_id
        FROM dbo_professional360.SIC_Preferences P
        JOIN dbo_professional360.Company C ON C.SIC_Id = P.SIC_Id
        WHERE C.CompanyId = @CompanyId;

    RETURN ISNULL((
        SELECT
            MC.metric_category_Id AS id,
            MC.categoryName AS name,
            JSON_QUERY((
                SELECT M.metric_id AS id, M.metric_name AS title, M.subtitle
                FROM dbo_professional360.metrics M
                JOIN @Chosen X ON X.metric_id = M.metric_id
                WHERE M.metric_category_Id = MC.metric_category_Id
                ORDER BY M.metric_id
                FOR JSON PATH)) AS selections
        FROM dbo_professional360.metric_category MC
        WHERE EXISTS (SELECT 1 FROM dbo_professional360.metrics M JOIN @Chosen X ON X.metric_id = M.metric_id
                      WHERE M.metric_category_Id = MC.metric_category_Id)
        ORDER BY MC.metric_category_Id
        FOR JSON PATH), N'[]');
END;
GO

-- GET api/companies/{id} : CompanyDetail (one row). keyTakeaways (review A1) and the bank
-- module flags (A5) are proposed additions. The BFF records the view (UserHistory) separately.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyDetails (@UserId INT, @CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        C.CompanyId AS id,
        C.CompanyName AS name,
        C.NameAbbreviation AS ticker,
        S.Description AS industry,
        C.logo,
        C.Location AS location,
        C.UpdatedAt AS updatedAt,
        dbo_professional360.udf_IsFavorityCompanyOfUser(@UserId, C.CompanyId) AS isFavorite,
        dbo_professional360.udf_IsCompanyPinnedForUser(@UserId, C.CompanyId) AS isPinned,
        dbo_professional360.udf_GetCompanyMetricSelectionJson(C.CompanyId) AS metricSelections,
        dbo_professional360.udf_GetUserMetricSelectionJson(@UserId, C.CompanyId) AS userMetricSelections,
        ISNULL((SELECT T.Ordinal AS ordinal, T.Takeaway AS [text]
                FROM dbo_professional360.KeyTakeaways T
                WHERE T.CompanyId = C.CompanyId AND T.isActive = 1
                ORDER BY T.Ordinal
                FOR JSON PATH), N'[]') AS keyTakeaways,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM dbo_professional360.LoanPortfolio L
                               WHERE L.CompanyId = C.CompanyId) THEN 1 ELSE 0 END AS BIT)
            AS hasLoanPortfolio,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM dbo_professional360.Trend T
                               WHERE T.CompanyId = C.CompanyId) THEN 1 ELSE 0 END AS BIT)
            AS hasPerformanceTrends
    FROM dbo_professional360.Company C
    LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
    WHERE C.CompanyId = @CompanyId
);
GO

-- GET api/companies/{id}/keyInsights : KeyInsight. source / link = the first source (as in the
-- front-end list); sources = all of them (review B1); relations from ItemLink (B3).
-- Order by: id.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyInsights (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        K.UniqueID AS id,
        K.title,
        K.summary,
        KT.TypeName AS [type],
        K.iconType,
        K.underwriting,
        K.evidence,
        JSON_VALUE(K.source, '$[0].title') AS source,
        K.link,
        K.source AS sources,
        K.updatedDate,
        K.confidence,
        dbo_professional360.udf_GetItemRelationsJson(K.CompanyId, N'KEY_INSIGHT', K.UniqueID) AS relations
    FROM dbo_professional360.KeyInsights K
    LEFT JOIN dbo_professional360.KeyInsightsTypes KT ON KT.KeyInsightsType_Id = K.KeyInsightsType_Id
    WHERE K.CompanyId = @CompanyId AND K.isActive = 1
);
GO

-- GET api/companies/{id}/riskInsights : RiskInsightRecord. category is the page group label;
-- order by: categoryOrder, id (Governance & Leadership, Risk & Legal, Event Signals, Behavioral).
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyRisks (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        R.StrategicRiskId AS id,
        R.Title AS title,
        R.Summary AS summary,
        RT.RiskTypeName AS [type],
        CASE R.Category
            WHEN N'governance' THEN N'Governance & Leadership'
            WHEN N'risk' THEN N'Risk & Legal'
            WHEN N'event' THEN N'Event Signals'
            WHEN N'behavior' THEN N'Behavioral Insights'
            ELSE R.Category END AS category,
        CASE R.Category
            WHEN N'governance' THEN 1 WHEN N'risk' THEN 2 WHEN N'event' THEN 3
            WHEN N'behavior' THEN 4 ELSE 5 END AS categoryOrder,
        R.UnderwritingRelevance AS underwriting,
        R.SupportingEvidence AS evidence,
        JSON_VALUE(R.sources, '$[0].title') AS source,
        JSON_VALUE(R.sources, '$[0].url') AS link,
        R.sources,
        R.UpdatedAt AS updatedDate,
        dbo_professional360.udf_GetItemRelationsJson(R.CompanyId, N'STRATEGIC_RISK', R.StrategicRiskId)
            AS relations
    FROM dbo_professional360.StrategicRisk R
    LEFT JOIN dbo_professional360.RiskTypes RT ON RT.RiskType_Id = R.RiskType_Id
    WHERE R.CompanyId = @CompanyId AND R.isActive = 1
);
GO

-- GET api/companies/{id}/followUps : FollowUpRecord. subtitle = why ask (summary); evidence =
-- underwriting relevance (review B4, to confirm). Order by: priorityOrder, id.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyFollowUpQuestions (@UserId INT, @CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        Q.FollowUpQuestionId AS id,
        Q.title,
        Q.summary AS subtitle,
        Q.category AS [type],
        Q.followingRisk AS risk,
        Q.underwritingRelevance AS evidence,
        Q.Priority AS priority,
        CASE Q.Priority WHEN N'high' THEN 1 WHEN N'medium' THEN 2 WHEN N'low' THEN 3 ELSE 4 END
            AS priorityOrder,
        CAST(CASE WHEN UF.FollowUpQuestionId IS NULL THEN 0 ELSE 1 END AS BIT) AS isFlagged,
        Q.sources,
        dbo_professional360.udf_GetItemRelationsJson(Q.CompanyId, N'FOLLOWUP', Q.FollowUpQuestionId)
            AS relations
    FROM dbo_professional360.FollowUpQuestions Q
    LEFT JOIN dbo_professional360.UserFlaggedFollowupQuestions UF
      ON UF.FollowUpQuestionId = Q.FollowUpQuestionId AND UF.UserId = @UserId
    WHERE Q.CompanyId = @CompanyId AND Q.isActive = 1
);
GO

-- GET api/companies/{id}/activities : Activity, with the latest news risk score (review B9).
-- @Days NULL = all. Order by: updatedDate DESC.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyActivities (@CompanyId INT, @Days INT)
RETURNS TABLE
AS RETURN (
    SELECT
        A.ActivityId AS id,
        A.title,
        A.summary,
        A.sentiment,
        A.underwriting,
        A.updateDate AS updatedDate,
        A.sources,
        A.confidence,
        S.overallRiskScore AS riskScore,
        S.litigationEO AS riskLitigation,
        S.regulatory AS riskRegulatory,
        S.financialDistress AS riskFinancialDistress,
        S.governance AS riskGovernance,
        S.operational AS riskOperational,
        S.rationale AS riskRationale
    FROM dbo_professional360.Activities A
    OUTER APPLY (SELECT TOP 1 N.*
                 FROM dbo_professional360.NewsRiskScore N
                 WHERE N.ActivityId = A.ActivityId
                 ORDER BY N.createdAt DESC, N.runId DESC) S
    WHERE A.CompanyId = @CompanyId
      AND (@Days IS NULL OR A.updateDate >= DATEADD(DAY, -@Days, CAST(SYSUTCDATETIME() AS DATETIME)))
);
GO

-- GET api/companies/{id}/categories/{c}/metrics/{m} : Metric (one row) for the card and the
-- modal (review A2). quarterly = last 8 quarters, annual = last 5 fiscal years, oldest first:
-- [{period:"FY2026 Q2", fiscalYear, fiscalPeriod, periodEnd, value, status, sourceLabel, notes}]
-- (value null when status = NM). methodology is newline-separated lines; dataSources and
-- dataPoints are JSON lists. The loan-portfolio modal uses the metric "Loan Portfolio
-- Composition".
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyMetric (@CompanyId INT, @MetricId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        M.metric_id AS id,
        M.metric_name AS name,
        MC.metric_category_Id AS categoryId,
        MC.categoryName,
        M.metric_name AS title,
        M.subtitle,
        M.description,
        M.Formula AS formula,
        (SELECT TOP 1 D.unit FROM dbo_professional360.MatrixData D
         WHERE D.CompanyId = @CompanyId AND D.metric_id = M.metric_id
         ORDER BY D.period_end DESC) AS unit,
        (SELECT x.period, x.fiscalYear, x.fiscalPeriod, x.periodEnd, x.value, x.status,
                x.sourceLabel, x.notes
         FROM (SELECT TOP 8
                   CONCAT(N'FY', D.fiscal_year, N' ', D.fiscal_period) AS period,
                   D.fiscal_year AS fiscalYear, D.fiscal_period AS fiscalPeriod,
                   D.period_end AS periodEnd,
                   CASE WHEN D.status = N'NM' THEN NULL ELSE D.value END AS value,
                   ISNULL(D.status, N'OK') AS status,
                   CASE D.source_class
                       WHEN N'PRELIMINARY' THEN N'Preliminary (earnings release)'
                       WHEN N'LLM_EXTRACTED' THEN N'Extracted from filing text'
                       WHEN N'DERIVED' THEN N'Derived'
                       WHEN N'COMPUTED' THEN N'Computed' END AS sourceLabel,
                   D.notes
               FROM dbo_professional360.MatrixData D
               WHERE D.CompanyId = @CompanyId AND D.metric_id = M.metric_id
                 AND D.fiscal_period LIKE N'Q%'
               ORDER BY D.period_end DESC) x
         ORDER BY x.periodEnd
         FOR JSON PATH, INCLUDE_NULL_VALUES) AS quarterly,
        (SELECT x.period, x.fiscalYear, x.fiscalPeriod, x.periodEnd, x.value, x.status,
                x.sourceLabel, x.notes
         FROM (SELECT TOP 5
                   CONCAT(N'FY', D.fiscal_year) AS period,
                   D.fiscal_year AS fiscalYear, D.fiscal_period AS fiscalPeriod,
                   D.period_end AS periodEnd,
                   CASE WHEN D.status = N'NM' THEN NULL ELSE D.value END AS value,
                   ISNULL(D.status, N'OK') AS status,
                   CASE D.source_class
                       WHEN N'PRELIMINARY' THEN N'Preliminary (earnings release)'
                       WHEN N'LLM_EXTRACTED' THEN N'Extracted from filing text'
                       WHEN N'DERIVED' THEN N'Derived'
                       WHEN N'COMPUTED' THEN N'Computed' END AS sourceLabel,
                   D.notes
               FROM dbo_professional360.MatrixData D
               WHERE D.CompanyId = @CompanyId AND D.metric_id = M.metric_id
                 AND D.fiscal_period = N'FY'
               ORDER BY D.period_end DESC) x
         ORDER BY x.periodEnd
         FOR JSON PATH, INCLUDE_NULL_VALUES) AS annual,
        CD.Methodology AS methodology,
        CD.DataSources AS dataSources,
        CD.DataPoints AS dataPoints,
        CD.UnderwritingRelevance AS underwritingRelevance,
        CD.UpdatedAt AS detailUpdatedAt,
        dbo_professional360.udf_GetItemRelationsJson(@CompanyId, N'METRIC', M.metric_id) AS relations
    FROM dbo_professional360.metrics M
    LEFT JOIN dbo_professional360.metric_category MC ON MC.metric_category_Id = M.metric_category_Id
    LEFT JOIN dbo_professional360.CompanyMetricDetail CD
      ON CD.CompanyId = @CompanyId AND CD.metric_id = M.metric_id
    WHERE M.metric_id = @MetricId
);
GO

-- GET api/companies/{id}/trends[/{trendId}] : TrendChartRecord, one row per trend (banks).
-- @TrendTypeId NULL = every trend (review A4). data: [{year, value, dataSources}], oldest first.
-- Order by: id.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyTrend (@CompanyId INT, @TrendTypeId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        TT.TrendTypeId AS id,
        TT.TrendName AS title,
        TT.Methodology AS methodology,
        (SELECT T.[Year] AS [year], T.[Value] AS value, JSON_QUERY(T.DataSources) AS dataSources
         FROM dbo_professional360.Trend T
         WHERE T.CompanyId = @CompanyId AND T.TrendTypeId = TT.TrendTypeId
         ORDER BY T.[Year]
         FOR JSON PATH, INCLUDE_NULL_VALUES) AS data
    FROM dbo_professional360.TrendTypes TT
    WHERE (@TrendTypeId IS NULL OR TT.TrendTypeId = @TrendTypeId)
      AND EXISTS (SELECT 1 FROM dbo_professional360.Trend T
                  WHERE T.CompanyId = @CompanyId AND T.TrendTypeId = TT.TrendTypeId)
);
GO

-- GET api/companies/{id}/loanPortfolio : one row per loan class and period (banks). periodRank 1
-- = current period end, 2 = prior year end. The modal's methodology and sources: udf_GetCompanyMetric
-- with the "Loan Portfolio Composition" metric. Order by: periodRank, parentSegment, segment.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyLoanPortfolios (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        L.LoanPortfolioId AS id,
        LT.LoanTypeName AS segment,
        LT.ParentSegment AS parentSegment,
        L.PeriodEnd AS periodEnd,
        DENSE_RANK() OVER (ORDER BY L.PeriodEnd DESC) AS periodRank,
        L.LoanValue AS value,
        L.PctOfBook AS pctOfBook,
        L.Nonaccrual AS nonaccrual,
        L.NonaccrualRatio AS nonaccrualRatio,
        L.UpdatedAt AS updatedAt
    FROM dbo_professional360.LoanPortfolio L
    JOIN dbo_professional360.LoanTypes LT ON LT.LoanTypeId = L.LoanTypeId
    WHERE L.CompanyId = @CompanyId
);
GO

-- POST api/companies/{id}/preferences/export : the user's metric choice for this company (else
-- the industry default) with each metric's latest value. Order by: categoryId, metricId.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyPerferences (@UserId INT, @CompanyId INT)
RETURNS @Out TABLE (
    categoryId INT, categoryName NVARCHAR(60), metricId INT, metricName NVARCHAR(120),
    subtitle NVARCHAR(200), isUserChoice BIT, latestPeriod NVARCHAR(20), latestPeriodEnd DATE,
    latestValue FLOAT, unit NVARCHAR(20), status NVARCHAR(10)
)
AS BEGIN
    DECLARE @Own BIT = CASE WHEN EXISTS (
        SELECT 1 FROM dbo_professional360.UserMetricsSelection
        WHERE UserId = @UserId AND CompanyId = @CompanyId) THEN 1 ELSE 0 END;
    DECLARE @Chosen TABLE (metric_id INT PRIMARY KEY);
    IF @Own = 1
        INSERT INTO @Chosen SELECT DISTINCT metric_id FROM dbo_professional360.UserMetricsSelection
        WHERE UserId = @UserId AND CompanyId = @CompanyId AND Selected = 1;
    ELSE
        INSERT INTO @Chosen SELECT DISTINCT P.metric_id
        FROM dbo_professional360.SIC_Preferences P
        JOIN dbo_professional360.Company C ON C.SIC_Id = P.SIC_Id
        WHERE C.CompanyId = @CompanyId;

    INSERT INTO @Out
    SELECT MC.metric_category_Id, MC.categoryName, M.metric_id, M.metric_name, M.subtitle, @Own,
           CASE WHEN D.fiscal_period = N'FY' THEN CONCAT(N'FY', D.fiscal_year)
                ELSE CONCAT(N'FY', D.fiscal_year, N' ', D.fiscal_period) END,
           D.period_end,
           CASE WHEN D.status = N'NM' THEN NULL ELSE D.value END,
           D.unit, ISNULL(D.status, N'OK')
    FROM @Chosen X
    JOIN dbo_professional360.metrics M ON M.metric_id = X.metric_id
    LEFT JOIN dbo_professional360.metric_category MC ON MC.metric_category_Id = M.metric_category_Id
    OUTER APPLY (SELECT TOP 1 D.*
                 FROM dbo_professional360.MatrixData D
                 WHERE D.CompanyId = @CompanyId AND D.metric_id = M.metric_id
                 -- the latest period; a quarter before the fiscal year ending the same day
                 ORDER BY D.period_end DESC, CASE WHEN D.fiscal_period = N'FY' THEN 1 ELSE 0 END) D;
    RETURN;
END;
GO
