-- Key Takeaways for the BFF (2026-10-28), to add on its own; also in bff_functions.sql.
-- GET api/companies/{id}/keyInsights/summary already reads udf_GetCompanyKeyInsightsSummary
-- (KeyInsightsSummaryId AS id, summary, updatedDate); this version reads the pipeline's
-- KeyTakeaways table (D-053) instead, so the BFF code does not change. It replaces the earlier
-- definition of the function (dropped first when that is not an inline function; script it out
-- beforehand to keep a copy). Schema [dbo_professional360]; if the tables are in [dbo], replace
-- "dbo_professional360." with "dbo." throughout. Needs SQL Server 2017+ / Azure SQL (STRING_AGG).

-- The BFF's earlier definition of this function is not an inline table function, and CREATE OR
-- ALTER cannot change a function's kind (error 2010): drop it first when it is of another kind.
IF EXISTS (SELECT 1 FROM sys.objects
           WHERE object_id = OBJECT_ID(N'dbo_professional360.udf_GetCompanyKeyInsightsSummary')
             AND type <> 'IF')
    DROP FUNCTION dbo_professional360.udf_GetCompanyKeyInsightsSummary;
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
