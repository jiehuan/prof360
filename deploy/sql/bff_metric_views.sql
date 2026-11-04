-- One Revenue Growth card (D-078, 2026-11-03), to add on its own; the functions are also in
-- bff_functions.sql. Revenue Growth (QoQ) becomes the QoQ view of the Revenue Growth card
-- (formerly Revenue Growth (YoY)) instead of a card of its own:
--   metrics.parentMetricId / cardView (deploy/sql/app_schema_changes.sql, run that first) mark a
--   metric that is a view of another metric's card;
--   udf_GetCompanyMetric returns the card's views (views[].quarterly for the QoQ button);
--   the metric pickers and the preferences export leave views out, and a user's earlier choice
--   of Revenue Growth (QoQ) counts as the Revenue Growth card.
-- Order on Azure: app_schema_changes.sql, this file, then the data (copy_sqlite_to_azure, sync).
-- Schema [dbo_professional360]; if the tables are in [dbo], replace "dbo_professional360." with
-- "dbo." throughout. Needs SQL Server 2016+ / Azure SQL (FOR JSON).

-- metricSelections: the metrics this company has values for, by category (the choices offered).
-- MetricSelectionCategory[] JSON; the loan-portfolio pseudo metric is left out (own module), and
-- so are views of another metric's card (parentMetricId set, D-078: Revenue Growth's QoQ).
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyMetricSelectionJson (@CompanyId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @Have TABLE (metric_id INT PRIMARY KEY);
    INSERT INTO @Have (metric_id)
    SELECT DISTINCT D.metric_id
    FROM dbo_professional360.MatrixData D
    JOIN dbo_professional360.metrics M ON M.metric_id = D.metric_id
    WHERE D.CompanyId = @CompanyId AND M.metric_name <> N'Loan Portfolio Composition'
      AND M.parentMetricId IS NULL;

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
-- no choice there, the industry default (SIC_Preferences). A chosen view of another metric's
-- card (D-078: Revenue Growth (QoQ), chosen before the cards were merged) counts as that card.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetUserMetricSelectionJson (@UserId INT, @CompanyId INT)
RETURNS NVARCHAR(MAX)
AS BEGIN
    DECLARE @SicId INT = (SELECT SIC_Id FROM dbo_professional360.Company WHERE CompanyId = @CompanyId);
    DECLARE @Chosen TABLE (metric_id INT PRIMARY KEY);
    INSERT INTO @Chosen (metric_id)
    SELECT DISTINCT ISNULL(M.parentMetricId, M.metric_id)
    FROM dbo_professional360.UserMetricsSelection U
    JOIN dbo_professional360.metrics M ON M.metric_id = U.metric_id
    WHERE U.UserId = @UserId AND U.SIC_Id = @SicId;
    IF NOT EXISTS (SELECT 1 FROM @Chosen)
        INSERT INTO @Chosen (metric_id)
        SELECT DISTINCT ISNULL(M.parentMetricId, M.metric_id)
        FROM dbo_professional360.SIC_Preferences P
        JOIN dbo_professional360.metrics M ON M.metric_id = P.metric_id
        WHERE P.SIC_Id = @SicId;

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

-- GET api/companies/{id}/categories/{c}/metrics/{m} : Metric (one row) for the card and the
-- modal (review A2). quarterly = last 8 quarters, annual = last 5 fiscal years, oldest first:
-- [{period:"FY2026 Q2", fiscalYear, fiscalPeriod, periodEnd, value, status, sourceLabel, notes}]
-- (value null when status = NM). methodology is newline-separated lines; dataSources and
-- dataPoints are JSON lists. The loan-portfolio modal uses the metric "Loan Portfolio
-- Composition". views (D-078): the card's other views, null when it has none:
-- [{view:"QoQ", metricId, title, subtitle, quarterly:[...]}] (Revenue Growth: QoQ button =
-- views[QoQ].quarterly, YoY = quarterly, 5Ys = annual). parentMetricId / cardView are set when
-- @MetricId is itself a view of another card (then use that card's id).
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
        (SELECT V.cardView AS [view], V.metric_id AS metricId, V.metric_name AS title, V.subtitle,
                JSON_QUERY((
                    SELECT x.period, x.fiscalYear, x.fiscalPeriod, x.periodEnd, x.value, x.status,
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
                          WHERE D.CompanyId = @CompanyId AND D.metric_id = V.metric_id
                            AND D.fiscal_period LIKE N'Q%'
                          ORDER BY D.period_end DESC) x
                    ORDER BY x.periodEnd
                    FOR JSON PATH, INCLUDE_NULL_VALUES)) AS quarterly
         FROM dbo_professional360.metrics V
         WHERE V.parentMetricId = M.metric_id
         ORDER BY V.metric_id
         FOR JSON PATH, INCLUDE_NULL_VALUES) AS views,
        M.parentMetricId AS parentMetricId,
        M.cardView AS cardView,
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

-- POST api/companies/{id}/preferences/export : the user's metric choice for this company's
-- industry (else the industry default) with each metric's latest value; a chosen view of another
-- metric's card counts as that card (D-078). Order by: categoryId, metricId.
CREATE OR ALTER FUNCTION dbo_professional360.udf_GetCompanyPreferences (@UserId INT, @CompanyId INT)
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
        INSERT INTO @Chosen SELECT DISTINCT ISNULL(M.parentMetricId, M.metric_id)
        FROM dbo_professional360.UserMetricsSelection U
        JOIN dbo_professional360.metrics M ON M.metric_id = U.metric_id
        WHERE U.UserId = @UserId AND U.SIC_Id = @SicId;
    ELSE
        INSERT INTO @Chosen SELECT DISTINCT ISNULL(M.parentMetricId, M.metric_id)
        FROM dbo_professional360.SIC_Preferences P
        JOIN dbo_professional360.metrics M ON M.metric_id = P.metric_id
        WHERE P.SIC_Id = @SicId;

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
