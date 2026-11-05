-- Follow-up questions for the BFF (2026-11-05), to add on its own; also in bff_functions.sql.
-- The BFF team's version adds readableSources: the titles of the question's sources
-- ([{title, url, date}] in FollowUpQuestions.sources) joined with " | ", in their stored order;
-- null when the question has no sources. Run with scripts/databricks/run_sql_script.py
-- (sql_file = deploy/sql/bff_followups.sql) or SSMS. Schema [dbo_professional360]; if the
-- tables are in [dbo], replace "dbo_professional360." with "dbo." throughout. Needs SQL Server
-- 2017+ / Azure SQL (STRING_AGG ... WITHIN GROUP).

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
        S.readableSources,
        dbo_professional360.udf_GetItemRelationsJson(Q.CompanyId, N'FOLLOWUP', Q.FollowUpQuestionId)
            AS relations
    FROM dbo_professional360.FollowUpQuestions Q
    OUTER APPLY (
        SELECT STRING_AGG(CAST(JSON_VALUE(J.[value], '$.title') AS NVARCHAR(MAX)), N' | ')
                   WITHIN GROUP (ORDER BY CAST(J.[key] AS INT)) AS readableSources
        FROM OPENJSON(Q.sources) J
    ) S
    LEFT JOIN dbo_professional360.UserFlaggedFollowupQuestions UF
      ON UF.FollowUpQuestionId = Q.FollowUpQuestionId AND UF.UserId = @UserId
    WHERE Q.CompanyId = @CompanyId AND Q.isActive = 1
);
GO
