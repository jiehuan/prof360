-- Data points with their numbers (2026-10-27): converts the DataPoints JSON already in the
-- application tables to the format publish writes from now on (docs/frontend-api-review.md A2).
-- Each point keeps its label and text and gets the numbers it shows, taken from this database's
-- own tables, so they equal what the front end reads elsewhere:
--   CompanyMetricDetail (metrics) and Trend: fiscalYear, fiscalPeriod ("Q2" / "FY"), periodEnd,
--     value, unit, status ("OK" / "NM"; value null when NM) from MatrixData; source = the
--     derivation named in the text's parentheses.
--   CompanyMetricDetail (Loan Portfolio Composition): segment, parentSegment, value, pctOfBook,
--     nonaccrual, nonaccrualRatio, priorValue, change, unit, periodEnd, priorPeriodEnd from
--     LoanPortfolio; the "Total" point covers the whole book.
-- Rows already in the new format are skipped: safe to run more than once. No table changes.
-- Schema [dbo_professional360]; if the tables are in [dbo], replace "dbo_professional360."
-- with "dbo." throughout. Needs SQL Server 2016+ / Azure SQL (OPENJSON, FOR JSON).
-- Tested 2026-10-27 on SQL Server 2022 (Marriott, Pfizer, JPMorgan): 72 metric modals, 45 trend
-- rows and JPMorgan's loan portfolio converted, a second run changed nothing; every value and
-- unit equal to MatrixData / Trend, the loan numbers equal to what publish writes and the book
-- ratio equal to udf_GetCompanyLoanQualitySummary. FOR JSON writes large numbers in exponent
-- form (1.6915e+010), which is valid JSON.

-- 1. Metric modals (every metric but the loan portfolio) ------------------------------------
UPDATE D SET D.DataPoints = X.points
FROM dbo_professional360.CompanyMetricDetail D
JOIN dbo_professional360.metrics M ON M.metric_id = D.metric_id
CROSS APPLY (
    SELECT (
        SELECT
            P.label, P.[text],
            ISNULL(MD.fiscal_year, P.fy) AS fiscalYear,
            P.fp AS fiscalPeriod,
            CONVERT(NCHAR(10), MD.period_end, 23) AS periodEnd,
            CASE WHEN MD.status = N'NM' THEN NULL ELSE MD.value END AS value,
            MD.unit,
            CASE WHEN MD.status = N'NM' OR MD.value IS NULL THEN N'NM' ELSE N'OK' END AS status,
            CASE WHEN RIGHT(P.[text], 1) = N')' AND CHARINDEX(N'(', P.[text]) > 0
                 THEN SUBSTRING(P.[text], LEN(P.[text]) - CHARINDEX(N'(', REVERSE(P.[text])) + 2,
                                CHARINDEX(N'(', REVERSE(P.[text])) - 2) END AS source
        FROM OPENJSON(D.DataPoints) J
        CROSS APPLY OPENJSON(J.value) WITH (label NVARCHAR(40), [text] NVARCHAR(400)) P0
        CROSS APPLY (SELECT P0.label, P0.[text],
                            TRY_CAST(SUBSTRING(P0.label, 3, 4) AS INT) AS fy,
                            CASE WHEN LEN(P0.label) > 6 THEN RIGHT(P0.label, 2) ELSE N'FY' END
                                AS fp) P
        OUTER APPLY (SELECT TOP 1 X.fiscal_year, X.period_end, X.value, X.unit, X.status
                     FROM dbo_professional360.MatrixData X
                     WHERE X.CompanyId = D.CompanyId AND X.metric_id = D.metric_id
                       AND X.fiscal_year = P.fy AND X.fiscal_period = P.fp
                     ORDER BY X.period_end DESC) MD
        ORDER BY CAST(J.[key] AS INT)
        FOR JSON PATH, INCLUDE_NULL_VALUES
    ) AS points
) X
WHERE M.metric_name <> N'Loan Portfolio Composition'
  AND ISJSON(D.DataPoints) = 1
  AND JSON_VALUE(D.DataPoints, '$[0].fiscalYear') IS NULL
  AND X.points IS NOT NULL;
GO

-- 2. Performance trends (banks) --------------------------------------------------------------
UPDATE T SET T.DataPoints = X.points
FROM dbo_professional360.Trend T
JOIN dbo_professional360.TrendTypes TT ON TT.TrendTypeId = T.TrendTypeId
OUTER APPLY (SELECT TOP 1 M.metric_id FROM dbo_professional360.metrics M
             WHERE M.metric_name = TT.TrendName) MM
CROSS APPLY (
    SELECT (
        SELECT
            P.label, P.[text],
            T.[Year] AS fiscalYear, N'FY' AS fiscalPeriod,
            CONVERT(NCHAR(10), MD.period_end, 23) AS periodEnd,
            CASE WHEN MD.status = N'NM' THEN NULL ELSE ISNULL(MD.value, T.[Value]) END AS value,
            MD.unit,
            CASE WHEN MD.status = N'NM' OR ISNULL(MD.value, T.[Value]) IS NULL THEN N'NM'
                 ELSE N'OK' END AS status,
            CAST(NULL AS NVARCHAR(100)) AS source,
            P.evidence
        FROM OPENJSON(T.DataPoints) J
        CROSS APPLY OPENJSON(J.value)
            WITH (label NVARCHAR(40), [text] NVARCHAR(400), evidence NVARCHAR(1000)) P
        OUTER APPLY (SELECT TOP 1 X.period_end, X.value, X.unit, X.status
                     FROM dbo_professional360.MatrixData X
                     WHERE X.CompanyId = T.CompanyId AND X.metric_id = MM.metric_id
                       AND X.fiscal_year = T.[Year] AND X.fiscal_period = N'FY'
                     ORDER BY X.period_end DESC) MD
        ORDER BY CAST(J.[key] AS INT)
        FOR JSON PATH, INCLUDE_NULL_VALUES
    ) AS points
) X
WHERE ISJSON(T.DataPoints) = 1
  AND JSON_VALUE(T.DataPoints, '$[0].fiscalYear') IS NULL
  AND X.points IS NOT NULL;
GO

-- 3. Loan Portfolio modal (banks) ----------------------------------------------------------
-- The latest period end and the one before it (the prior year-end shown with it), per company.
UPDATE D SET D.DataPoints = X.points
FROM dbo_professional360.CompanyMetricDetail D
JOIN dbo_professional360.metrics M ON M.metric_id = D.metric_id
CROSS APPLY (SELECT MAX(L.PeriodEnd) AS latest FROM dbo_professional360.LoanPortfolio L
             WHERE L.CompanyId = D.CompanyId) LA
CROSS APPLY (SELECT MAX(L.PeriodEnd) AS prior FROM dbo_professional360.LoanPortfolio L
             WHERE L.CompanyId = D.CompanyId AND L.PeriodEnd < LA.latest) PR
CROSS APPLY (
    -- the whole book at the latest period end (nonaccrual: the classes that disclose it)
    SELECT
        SUM(CAST(L.LoanValue AS FLOAT)) AS value,
        CASE WHEN SUM(CASE WHEN L.Nonaccrual IS NULL THEN 0 ELSE 1 END) > 0
             THEN SUM(CASE WHEN L.Nonaccrual IS NULL THEN 0.0 ELSE CAST(L.Nonaccrual AS FLOAT) END)
             END AS nonaccrual,
        SUM(CASE WHEN L.Nonaccrual IS NULL THEN 0.0 ELSE CAST(L.LoanValue AS FLOAT) END)
            AS reported
    FROM dbo_professional360.LoanPortfolio L
    WHERE L.CompanyId = D.CompanyId AND L.PeriodEnd = LA.latest
) B
CROSS APPLY (
    SELECT SUM(CAST(L.LoanValue AS FLOAT)) AS priorValue
    FROM dbo_professional360.LoanPortfolio L
    WHERE L.CompanyId = D.CompanyId AND L.PeriodEnd = PR.prior
) BP
CROSS APPLY (
    SELECT (
        SELECT
            P.label, P.[text], P.evidence,
            CASE WHEN P.label = N'Total' AND C.LoanValue IS NULL THEN NULL
                 ELSE C.LoanTypeName END AS segment,
            CASE WHEN P.label = N'Total' AND C.LoanValue IS NULL THEN NULL
                 ELSE C.ParentSegment END AS parentSegment,
            N.value, N.pctOfBook, N.nonaccrual, N.nonaccrualRatio, N.priorValue,
            N.value / NULLIF(N.priorValue, 0) - 1 AS change,
            N'USD' AS unit,
            CONVERT(NCHAR(10), LA.latest, 23) AS periodEnd,
            CONVERT(NCHAR(10), PR.prior, 23) AS priorPeriodEnd
        FROM OPENJSON(D.DataPoints) J
        CROSS APPLY OPENJSON(J.value)
            WITH (label NVARCHAR(200), [text] NVARCHAR(1000), evidence NVARCHAR(1000)) P
        OUTER APPLY (SELECT TOP 1 CAST(L.LoanValue AS FLOAT) AS LoanValue,
                                  CAST(L.Nonaccrual AS FLOAT) AS Nonaccrual,
                                  LT.LoanTypeName, LT.ParentSegment, L.LoanTypeId
                     FROM dbo_professional360.LoanPortfolio L
                     JOIN dbo_professional360.LoanTypes LT ON LT.LoanTypeId = L.LoanTypeId
                     WHERE L.CompanyId = D.CompanyId AND L.PeriodEnd = LA.latest
                       AND LT.LoanTypeName = P.label) C
        OUTER APPLY (SELECT TOP 1 CAST(L.LoanValue AS FLOAT) AS LoanValue
                     FROM dbo_professional360.LoanPortfolio L
                     WHERE L.CompanyId = D.CompanyId AND L.PeriodEnd = PR.prior
                       AND L.LoanTypeId = C.LoanTypeId) O
        CROSS APPLY (
            SELECT
                CASE WHEN C.LoanValue IS NOT NULL THEN C.LoanValue
                     WHEN P.label = N'Total' THEN B.value END AS value,
                CASE WHEN C.LoanValue IS NOT NULL THEN C.LoanValue / NULLIF(B.value, 0)
                     WHEN P.label = N'Total' AND B.value IS NOT NULL THEN 1.0 END AS pctOfBook,
                CASE WHEN C.LoanValue IS NOT NULL THEN C.Nonaccrual
                     WHEN P.label = N'Total' THEN B.nonaccrual END AS nonaccrual,
                CASE WHEN C.LoanValue IS NOT NULL THEN C.Nonaccrual / NULLIF(C.LoanValue, 0)
                     WHEN P.label = N'Total' THEN B.nonaccrual / NULLIF(B.reported, 0)
                     END AS nonaccrualRatio,
                CASE WHEN C.LoanValue IS NOT NULL THEN O.LoanValue
                     WHEN P.label = N'Total' THEN BP.priorValue END AS priorValue
        ) N
        ORDER BY CAST(J.[key] AS INT)
        FOR JSON PATH, INCLUDE_NULL_VALUES
    ) AS points
) X
WHERE M.metric_name = N'Loan Portfolio Composition'
  AND ISJSON(D.DataPoints) = 1
  AND JSON_VALUE(D.DataPoints, '$[0].unit') IS NULL
  AND LA.latest IS NOT NULL
  AND X.points IS NOT NULL;
GO
