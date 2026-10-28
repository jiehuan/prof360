-- udf_GetCompanyDetails for the BFF (2026-10-29), to add on its own; the same definition is in
-- bff_functions.sql. It is dv's version (with the trends column the BFF team added) with one
-- fix: trends now lists the company's trend type ids (TrendTypeId), the ids GET
-- api/companies/{id}/trends/{trendId} takes (udf_GetCompanyTrend), each once, in order. dv's
-- version listed Trend row ids (one per trend and year, e.g. 45 for JPMorgan's 5 trends).
-- STRING_AGG instead of JSON_ARRAYAGG: the same JSON array, and it also runs on SQL Server 2022.
-- Calls udf_IsFavoriteCompanyOfUser, udf_IsCompanyPinnedForUser,
-- udf_GetCompanyMetricSelectionJson and udf_GetUserMetricSelectionJson (all on dv already).
-- Schema [dbo_professional360]; if the tables are in [dbo], replace "dbo_professional360."
-- with "dbo." throughout.

-- GET api/companies/{id} : CompanyDetail (one row). keyTakeaways (review A1; the endpoint uses
-- udf_GetCompanyKeyInsightsSummary instead) and the bank module flags (A5) are proposed
-- additions; trends: the company's trend type ids, e.g. [1,2,3,4,5] (banks; [] otherwise).
-- The BFF records the view (UserHistory) separately.
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
        dbo_professional360.udf_IsFavoriteCompanyOfUser(@UserId, C.CompanyId) AS isFavorite,
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
            AS hasPerformanceTrends,
        ISNULL(TR.trends, N'[]') AS trends
    FROM dbo_professional360.Company C
    LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
    OUTER APPLY (
        SELECT N'[' + STRING_AGG(CAST(x.TrendTypeId AS NVARCHAR(12)), N',')
                          WITHIN GROUP (ORDER BY x.TrendTypeId) + N']' AS trends
        FROM (SELECT DISTINCT T.TrendTypeId FROM dbo_professional360.Trend T
              WHERE T.CompanyId = C.CompanyId) x
    ) TR
    WHERE C.CompanyId = @CompanyId
);
GO
