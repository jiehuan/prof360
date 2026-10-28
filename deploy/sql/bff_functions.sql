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
--     UserMetricsSelection (UserId, SIC_Id, metric_id): a user's metric choice per industry
--     (SIC), shared by the companies of that industry (bff_user_tables.sql, deployed on pp).
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
-- Loan quality (udf_GetLoanQuality, udf_GetLoanQualityTrend, udf_GetCompanyLoanQualitySummary)
-- tested 2026-10-26 on the same database: JPMorgan's classes and book as computed by hand,
-- band and trend boundaries (0.50 / 1.50 / 3.00%, 0.25 points) on both sides.
-- Notifications (udf_GetUserNotifications, udf_GetUserNotificationCount) tested 2026-10-27
-- on the same database; they read the user table UserNotificationRead (bff_notifications.sql).

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

-- Credit quality of a loan class (or a whole loan book) from its nonaccrual ratio (nonaccrual
-- loans / loans, a fraction): the one place the bands are set. NULL: the filing gives no
-- nonaccrual figure for the class (credit cards, charged off instead of placed on nonaccrual).
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetLoanQuality (@NonaccrualRatio FLOAT)
RETURNS NVARCHAR(20)
AS BEGIN
    RETURN CASE
        WHEN @NonaccrualRatio IS NULL THEN N'Not reported'
        WHEN @NonaccrualRatio < 0.005 THEN N'Strong'        -- under 0.50%
        WHEN @NonaccrualRatio < 0.015 THEN N'Satisfactory'  -- 0.50% to under 1.50%
        WHEN @NonaccrualRatio < 0.030 THEN N'Watch'         -- 1.50% to under 3.00%
        ELSE N'Weak' END;                                   -- 3.00% and over
END;
GO

-- Direction of a nonaccrual ratio against the prior year end: a move of at least 0.25
-- percentage points either way; NULL when either ratio is missing.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetLoanQualityTrend (
    @NonaccrualRatio FLOAT, @PriorRatio FLOAT
)
RETURNS NVARCHAR(20)
AS BEGIN
    RETURN CASE
        WHEN @NonaccrualRatio IS NULL OR @PriorRatio IS NULL THEN NULL
        WHEN @NonaccrualRatio - @PriorRatio >= 0.0025 THEN N'Deteriorating'
        WHEN @PriorRatio - @NonaccrualRatio >= 0.0025 THEN N'Improving'
        ELSE N'Stable' END;
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

-- userMetricSelections: the user's choice for this company's industry (SIC); if the user has
-- no choice there, the industry default (SIC_Preferences).
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetUserMetricSelectionJson (@UserId INT, @CompanyId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @SicId INT = (SELECT SIC_Id FROM dbo_professional360.Company WHERE CompanyId = @CompanyId);
    DECLARE @Chosen TABLE (metric_id INT PRIMARY KEY);
    INSERT INTO @Chosen (metric_id)
    SELECT DISTINCT metric_id FROM dbo_professional360.UserMetricsSelection
    WHERE UserId = @UserId AND SIC_Id = @SicId;
    IF NOT EXISTS (SELECT 1 FROM @Chosen)
        INSERT INTO @Chosen (metric_id)
        SELECT DISTINCT metric_id FROM dbo_professional360.SIC_Preferences WHERE SIC_Id = @SicId;

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

-- GET api/companies/{id} : CompanyDetail (one row). keyTakeaways (review A1; the endpoint uses
-- udf_GetCompanyKeyInsightsSummary instead) and the bank module flags (A5) are proposed additions. The BFF records the view (UserHistory) separately.
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

-- GET api/companies/{id}/keyInsights/summary : the Key Takeaway box above the Key Insights
-- (D-053), one row; none when the company has no current takeaways (the BFF then answers 404
-- and the box is hidden). Keeps the columns the BFF already selects from this function:
--   KeyInsightsSummaryId: the first current takeaway's id (changes with a new version);
--   summary: the 2-3 takeaways in order (Ordinal 1 = most important), one per line ("\n");
--   updatedDate: when they were generated;
--   takeaways: the same as JSON [{ordinal, text}], for a bulleted list without splitting.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyKeyInsightsSummary (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        x.KeyInsightsSummaryId,
        x.summary,
        x.updatedDate,
        (SELECT T.Ordinal AS ordinal, T.Takeaway AS [text]
         FROM dbo_professional360.KeyTakeaways T
         WHERE T.CompanyId = @CompanyId AND T.isActive = 1
         ORDER BY T.Ordinal
         FOR JSON PATH) AS takeaways
    FROM (
        SELECT
            MIN(T.KeyTakeawayId) AS KeyInsightsSummaryId,
            STRING_AGG(CAST(T.Takeaway AS NVARCHAR(MAX)), NCHAR(10))
                WITHIN GROUP (ORDER BY T.Ordinal) AS summary,
            CAST(MAX(T.UpdatedDate) AS DATETIME) AS updatedDate
        FROM dbo_professional360.KeyTakeaways T
        WHERE T.CompanyId = @CompanyId AND T.isActive = 1
        HAVING COUNT(*) > 0
    ) x
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

-- GET api/companies/{id}/loanPortfolio : one row per loan class and period (banks), with the
-- credit quality computed here: this one function is all the endpoint needs. periodRank 1 =
-- current period end, 2 = prior year end. The modal's methodology, sources and
-- raw data points: udf_GetCompanyLoanPortfolioDetail.
--   quality: the class's nonaccrual ratio (nonaccrual loans / loans): under 0.50% Strong, under
--     1.50% Satisfactory, under 3.00% Watch, else Weak; no figure: Not reported (credit cards
--     are charged off instead of placed on nonaccrual).
--   qualityTrend: against the same class at the prior period end: up 0.25 percentage points or
--     more Deteriorating, down as much Improving, else Stable; NULL on the prior period's rows
--     and when either ratio is missing.
--   book*: the whole loan book at the row's period end (the same on every row of a period):
--     bookNonaccrualRatio = nonaccrual over the loans of the classes that report it (the others
--     are left out of both sides, not counted as zero), bookReportedShare = the share of the
--     book behind that ratio, bookQuality and bookQualityTrend as above.
-- The bands are also in udf_GetLoanQuality / udf_GetLoanQualityTrend: change them together.
-- Order by: periodRank, parentSegment, segment.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyLoanPortfolios (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        x.id, x.segment, x.parentSegment, x.periodEnd, x.periodRank, x.value, x.pctOfBook,
        x.nonaccrual, x.nonaccrualRatio,
        CASE
            WHEN x.nonaccrualRatio IS NULL THEN N'Not reported'
            WHEN x.nonaccrualRatio < 0.005 THEN N'Strong'
            WHEN x.nonaccrualRatio < 0.015 THEN N'Satisfactory'
            WHEN x.nonaccrualRatio < 0.030 THEN N'Watch'
            ELSE N'Weak' END AS quality,
        CASE
            WHEN x.nonaccrualRatio IS NULL OR x.priorRatio IS NULL THEN NULL
            WHEN x.nonaccrualRatio - x.priorRatio >= 0.0025 THEN N'Deteriorating'
            WHEN x.priorRatio - x.nonaccrualRatio >= 0.0025 THEN N'Improving'
            ELSE N'Stable' END AS qualityTrend,
        x.updatedAt,
        b.nonaccrualRatio AS bookNonaccrualRatio,
        b.reportedShare AS bookReportedShare,
        CASE
            WHEN b.nonaccrualRatio IS NULL THEN N'Not reported'
            WHEN b.nonaccrualRatio < 0.005 THEN N'Strong'
            WHEN b.nonaccrualRatio < 0.015 THEN N'Satisfactory'
            WHEN b.nonaccrualRatio < 0.030 THEN N'Watch'
            ELSE N'Weak' END AS bookQuality,
        CASE
            WHEN b.nonaccrualRatio IS NULL OR b.priorRatio IS NULL THEN NULL
            WHEN b.nonaccrualRatio - b.priorRatio >= 0.0025 THEN N'Deteriorating'
            WHEN b.priorRatio - b.nonaccrualRatio >= 0.0025 THEN N'Improving'
            ELSE N'Stable' END AS bookQualityTrend
    FROM (
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
            LAG(L.NonaccrualRatio) OVER (PARTITION BY L.LoanTypeId ORDER BY L.PeriodEnd)
                AS priorRatio,
            L.UpdatedAt AS updatedAt
        FROM dbo_professional360.LoanPortfolio L
        JOIN dbo_professional360.LoanTypes LT ON LT.LoanTypeId = L.LoanTypeId
        WHERE L.CompanyId = @CompanyId
    ) x
    JOIN (
        SELECT
            a.periodEnd,
            a.nonaccrual / NULLIF(a.reportedLoans, 0) AS nonaccrualRatio,
            a.reportedLoans / NULLIF(a.totalLoans, 0) AS reportedShare,
            LAG(a.nonaccrual / NULLIF(a.reportedLoans, 0)) OVER (ORDER BY a.periodEnd)
                AS priorRatio
        FROM (
            SELECT
                L.PeriodEnd AS periodEnd,
                CAST(SUM(L.LoanValue) AS FLOAT) AS totalLoans,
                CAST(SUM(CASE WHEN L.Nonaccrual IS NOT NULL THEN L.LoanValue END) AS FLOAT)
                    AS reportedLoans,
                CAST(SUM(L.Nonaccrual) AS FLOAT) AS nonaccrual
            FROM dbo_professional360.LoanPortfolio L
            WHERE L.CompanyId = @CompanyId
            GROUP BY L.PeriodEnd
        ) a
    ) b ON b.periodEnd = x.periodEnd
);
GO

-- The loan book's credit quality, one row per period (periodRank 1 = current, 2 = prior year
-- end): nonaccrual over the loans of the classes that report it (classes without a figure are
-- left out of both sides, not counted as zero), its quality band and trend, and reportedShare =
-- the share of the book behind the ratio. Order by: periodRank.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyLoanQualitySummary (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT
        b.periodEnd,
        b.periodRank,
        b.totalLoans,
        b.nonaccrual,
        b.nonaccrualRatio,
        b.reportedShare,
        dbo_professional360.udf_GetLoanQuality(b.nonaccrualRatio) AS quality,
        dbo_professional360.udf_GetLoanQualityTrend(
            b.nonaccrualRatio, LEAD(b.nonaccrualRatio) OVER (ORDER BY b.periodEnd DESC)
        ) AS qualityTrend
    FROM (
        SELECT
            a.periodEnd,
            DENSE_RANK() OVER (ORDER BY a.periodEnd DESC) AS periodRank,
            a.totalLoans,
            a.nonaccrual,
            a.nonaccrual / NULLIF(a.reportedLoans, 0) AS nonaccrualRatio,
            a.reportedLoans / NULLIF(a.totalLoans, 0) AS reportedShare
        FROM (
            SELECT
                L.PeriodEnd AS periodEnd,
                CAST(SUM(L.LoanValue) AS FLOAT) AS totalLoans,
                CAST(SUM(CASE WHEN L.Nonaccrual IS NOT NULL THEN L.LoanValue END) AS FLOAT)
                    AS reportedLoans,
                CAST(SUM(L.Nonaccrual) AS FLOAT) AS nonaccrual
            FROM dbo_professional360.LoanPortfolio L
            WHERE L.CompanyId = @CompanyId
            GROUP BY L.PeriodEnd
        ) a
    ) b
);
GO

-- GET api/companies/{id}/loanPortfolio/detail : the Loan Portfolio Composition modal (banks),
-- one row; none when the company has no loan portfolio. The same content as udf_GetCompanyMetric
-- with the "Loan Portfolio Composition" metric, found by name so the endpoint needs no metric id.
--   methodology: text ("\n" separates lines).
--   dataSources: JSON [{title, url, date}].
--   dataPoints (Raw Data Points): JSON [{label, text, evidence, segment, parentSegment, value,
--     pctOfBook, nonaccrual, nonaccrualRatio, priorValue, change, unit, periodEnd,
--     priorPeriodEnd}]; show "label: text" per line; the first (label "Total") is the whole book.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyLoanPortfolioDetail (@CompanyId INT)
RETURNS TABLE
AS RETURN (
    SELECT TOP 1
        M.metric_id AS metricId,
        M.metric_name AS title,
        M.subtitle,
        CD.Methodology AS methodology,
        CD.DataSources AS dataSources,
        CD.DataPoints AS dataPoints,
        CD.UpdatedAt AS updatedAt
    FROM dbo_professional360.CompanyMetricDetail CD
    JOIN dbo_professional360.metrics M ON M.metric_id = CD.metric_id
    WHERE CD.CompanyId = @CompanyId AND M.metric_name = N'Loan Portfolio Composition'
    ORDER BY CD.UpdatedAt DESC
);
GO

-- POST api/companies/{id}/preferences/export : the user's metric choice for this company's
-- industry (else the industry default) with each metric's latest value. Order by: categoryId,
-- metricId.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyPerferences (@UserId INT, @CompanyId INT)
RETURNS @Out TABLE (
    categoryId INT, categoryName NVARCHAR(60), metricId INT, metricName NVARCHAR(120),
    subtitle NVARCHAR(200), isUserChoice BIT, latestPeriod NVARCHAR(20), latestPeriodEnd DATE,
    latestValue FLOAT, unit NVARCHAR(20), status NVARCHAR(10)
)
AS BEGIN
    DECLARE @SicId INT = (SELECT SIC_Id FROM dbo_professional360.Company WHERE CompanyId = @CompanyId);
    DECLARE @Own BIT = CASE WHEN EXISTS (
        SELECT 1 FROM dbo_professional360.UserMetricsSelection
        WHERE UserId = @UserId AND SIC_Id = @SicId) THEN 1 ELSE 0 END;
    DECLARE @Chosen TABLE (metric_id INT PRIMARY KEY);
    IF @Own = 1
        INSERT INTO @Chosen SELECT DISTINCT metric_id FROM dbo_professional360.UserMetricsSelection
        WHERE UserId = @UserId AND SIC_Id = @SicId;
    ELSE
        INSERT INTO @Chosen SELECT DISTINCT metric_id
        FROM dbo_professional360.SIC_Preferences WHERE SIC_Id = @SicId;

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

-- GET api/notifications : the bell on the home page (2026-10-27). Events of the last @Days days
-- (NULL = 30) for the companies the user follows: favorites and pins. One row per event, with
-- notificationKey (stable: what the read procedures take) and isRead / readAt from
-- UserNotificationRead. notificationType:
--   NEWS     a news event (Activities) with an article scored at least @MinNewsRisk overall
--            (NewsRiskScore, 0-1; NULL = 0.6: about 3% of events). itemId = ActivityId,
--            riskScore = the highest article score, link = the first source.
--   INSIGHT  a current key insight whose event date (updatedDate) is in the window.
--            itemId = KeyInsights.UniqueID, tag = Warning / Negative / Positive / Info.
--   SIGNAL   a current Event Signals item (StrategicRisk, Category 'event') dated in the window.
--            itemId = StrategicRiskId, tag = its risk type.
--   FILING   a 10-K or 10-Q (or amendment), or an 8-K earnings release, by filing date.
--            itemId NULL, tag = the form, link = the filing on SEC EDGAR.
-- Dates of insights and signals are the dates of the events they describe, so an insight
-- written again about an old event does not come back as new (outside the window). A key
-- insight or signal that is regenerated gets a new id, so it is unread again: a new version is
-- new content. Order by: eventDate DESC.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetUserNotifications (
    @UserId INT, @Days INT, @MinNewsRisk FLOAT
)
RETURNS TABLE
AS RETURN (
    SELECT
        e.notificationKey, e.notificationType, e.itemId,
        C.CompanyId AS companyId, C.CompanyName AS companyName, C.NameAbbreviation AS ticker,
        e.eventDate, e.title, e.summary, e.tag, e.riskScore, e.link,
        CAST(CASE WHEN UR.NotificationKey IS NULL THEN 0 ELSE 1 END AS BIT) AS isRead,
        UR.ReadAt AS readAt
    FROM (
        SELECT CompanyId FROM dbo_professional360.UserFavority WHERE UserId = @UserId
        UNION
        SELECT CompanyId FROM dbo_professional360.UserPinnedCompanies WHERE UserId = @UserId
    ) f
    JOIN dbo_professional360.Company C ON C.CompanyId = f.CompanyId
    CROSS APPLY (SELECT CAST(DATEADD(DAY, -ISNULL(@Days, 30), CAST(SYSUTCDATETIME() AS DATE))
                             AS DATETIME) AS since) w
    CROSS APPLY (
        SELECT CAST(CONCAT(N'NEWS:', A.ActivityId) AS NVARCHAR(100)) AS notificationKey,
               N'NEWS' AS notificationType, A.ActivityId AS itemId,
               CAST(A.updateDate AS DATETIME) AS eventDate, CAST(A.title AS NVARCHAR(1000)) AS title,
               CAST(A.summary AS NVARCHAR(MAX)) AS summary, CAST(NULL AS NVARCHAR(100)) AS tag,
               CAST(S.riskScore AS NUMERIC(5, 2)) AS riskScore,
               CAST(JSON_VALUE(A.sources, '$[0].url') AS NVARCHAR(2000)) AS link
        FROM dbo_professional360.Activities A
        CROSS APPLY (SELECT MAX(N.overallRiskScore) AS riskScore
                     FROM dbo_professional360.NewsRiskScore N
                     WHERE N.ActivityId = A.ActivityId) S
        WHERE A.CompanyId = f.CompanyId AND A.updateDate >= w.since
          AND S.riskScore >= ISNULL(@MinNewsRisk, 0.6)
        UNION ALL
        SELECT CONCAT(N'INSIGHT:', K.UniqueID), N'INSIGHT', K.UniqueID, CAST(K.updatedDate AS DATETIME), K.title, K.summary,
               T.TypeName, NULL, K.link
        FROM dbo_professional360.KeyInsights K
        LEFT JOIN dbo_professional360.KeyInsightsTypes T
          ON T.KeyInsightsType_Id = K.KeyInsightsType_Id
        WHERE K.CompanyId = f.CompanyId AND K.isActive = 1 AND K.updatedDate >= w.since
        UNION ALL
        SELECT CONCAT(N'SIGNAL:', R.StrategicRiskId), N'SIGNAL', R.StrategicRiskId, CAST(R.UpdatedAt AS DATETIME), R.Title, R.Summary,
               RT.RiskTypeName, NULL, JSON_VALUE(R.sources, '$[0].url')
        FROM dbo_professional360.StrategicRisk R
        LEFT JOIN dbo_professional360.RiskTypes RT ON RT.RiskType_Id = R.RiskType_Id
        WHERE R.CompanyId = f.CompanyId AND R.isActive = 1 AND R.Category = N'event'
          AND R.UpdatedAt >= w.since
        UNION ALL
        SELECT CONCAT(N'FILING:', g.accession), N'FILING', NULL, CAST(g.filedDate AS DATETIME),
               CONCAT(CASE WHEN g.form = N'8-K' THEN N'Earnings release (8-K)' ELSE g.form END,
                      N' for the period ended ', CONVERT(NCHAR(10), g.periodEnd, 23)),
               NULL, g.form, NULL,
               CONCAT(N'https://www.sec.gov/Archives/edgar/data/',
                      CAST(CAST(C.CIK AS BIGINT) AS NVARCHAR(20)), N'/',
                      REPLACE(g.accession, N'-', N''), N'/')
        FROM (
            -- one row per filing: values taken from a single filing name its accession number
            SELECT M.form, JSON_VALUE(M.accession, '$[0]') AS accession,
                   MAX(TRY_CONVERT(DATE, JSON_VALUE(M.filed_date, '$[0]'))) AS filedDate,
                   MAX(M.period_end) AS periodEnd
            FROM dbo_professional360.MatrixData M
            WHERE M.CompanyId = f.CompanyId
              AND M.form IN (N'10-K', N'10-Q', N'10-K/A', N'10-Q/A', N'8-K')
              AND M.accession NOT LIKE N'%,%'
            GROUP BY M.form, JSON_VALUE(M.accession, '$[0]')
        ) g
        WHERE g.filedDate >= w.since
    ) e
    LEFT JOIN dbo_professional360.UserNotificationRead UR
      ON UR.UserId = @UserId AND UR.NotificationKey = e.notificationKey
);
GO

-- GET api/notifications/count : the red dot. Same arguments as udf_GetUserNotifications, so the
-- number matches the list. One row: unreadCount, totalCount.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetUserNotificationCount (
    @UserId INT, @Days INT, @MinNewsRisk FLOAT
)
RETURNS TABLE
AS RETURN (
    SELECT
        COUNT(CASE WHEN n.isRead = 0 THEN 1 END) AS unreadCount,
        COUNT(*) AS totalCount
    FROM dbo_professional360.udf_GetUserNotifications(@UserId, @Days, @MinNewsRisk) n
);
GO
