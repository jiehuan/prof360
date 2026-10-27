-- Write procedures for the BFF (user state), one per stored-procedure file in
-- professional-databricks/data_io/azure_sql/sql_queries/stored_procedures (same names, spelling
-- kept), plus sp_UpdateUserMetricSelection for PUT .../metrics/selections. A proposal from the
-- pipeline side (2026-10-23); the BFF team owns the final version. Companion of
-- bff_functions.sql (the reads); open points: docs/frontend-api-review.md.
--
-- Assumptions (check before use):
--   * Schema [dbo_professional360]; if the tables are in [dbo], replace
--     "dbo_professional360." with "dbo." throughout.
--   * User tables as deployed on pp (bff_user_tables.sql): UserFavority (UserId, CompanyId,
--     CreatedAt), UserPinnedCompanies (UserId, CompanyId, PinnedAt), UserHistory (UserHistoryId,
--     UserId, CompanyId, ViewedAt), UserFlaggedFollowupQuestions (UserId, FollowUpQuestionId,
--     FlaggedAt), UserMetricsSelection (UserId, SIC_Id, metric_id). No soft-delete columns: a
--     removed favorite / pin / flag is a deleted row.
--   * Every procedure can be called twice with the same arguments (a double click, a retry)
--     without an error or a duplicate row.
--   * Errors the BFF should answer with 404 / 400 are raised with THROW 5xxxx:
--       50001 company not found, 50002 question not current (replaced or removed: reload the
--       list), 50003 category not found, 50004 metric not in the category, 50005 not a
--       notification key.
--   * The notification procedures write UserNotificationRead (UserId, NotificationKey, ReadAt),
--     created by bff_notifications.sql.
--
-- Tested 2026-10-23 on SQL Server 2022 together with bff_functions.sql.

-- GET api/companies/{id} records the view: one row per user and company, its time updated on
-- every view (Recently Viewed is ordered by it).
CREATE OR ALTER PROCEDURE dbo_professional360.sp_AddViewedCompanyRecord
    @UserId INT,
    @CompanyId INT
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT EXISTS (SELECT 1 FROM dbo_professional360.Company WHERE CompanyId = @CompanyId)
        THROW 50001, N'company not found', 1;
    BEGIN TRANSACTION;
    UPDATE dbo_professional360.UserHistory WITH (UPDLOCK, HOLDLOCK)
    SET ViewedAt = GETUTCDATE()
    WHERE UserId = @UserId AND CompanyId = @CompanyId;
    IF @@ROWCOUNT = 0
        INSERT INTO dbo_professional360.UserHistory (UserId, CompanyId, ViewedAt)
        VALUES (@UserId, @CompanyId, GETUTCDATE());
    COMMIT TRANSACTION;
END;
GO

-- DELETE api/companies/viewed/{company_id}
CREATE OR ALTER PROCEDURE dbo_professional360.sp_RemoveViewdCompanyRecord
    @UserId INT,
    @CompanyId INT
AS
BEGIN
    SET NOCOUNT ON;
    DELETE FROM dbo_professional360.UserHistory
    WHERE UserId = @UserId AND CompanyId = @CompanyId;
END;
GO

-- PUT api/companies/{company_id}/favorited/{bool}
CREATE OR ALTER PROCEDURE dbo_professional360.sp_UpdateCompanyFavoritedStatus
    @UserId INT,
    @CompanyId INT,
    @IsFavorited BIT
AS
BEGIN
    SET NOCOUNT ON;
    IF @IsFavorited = 1
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM dbo_professional360.Company WHERE CompanyId = @CompanyId)
            THROW 50001, N'company not found', 1;
        INSERT INTO dbo_professional360.UserFavority (UserId, CompanyId, CreatedAt)
        SELECT @UserId, @CompanyId, GETUTCDATE()
        WHERE NOT EXISTS (SELECT 1 FROM dbo_professional360.UserFavority WITH (UPDLOCK, HOLDLOCK)
                          WHERE UserId = @UserId AND CompanyId = @CompanyId);
    END
    ELSE
        DELETE FROM dbo_professional360.UserFavority
        WHERE UserId = @UserId AND CompanyId = @CompanyId;
END;
GO

-- PUT api/companies/{company_id}/pinned/{bool}
CREATE OR ALTER PROCEDURE dbo_professional360.sp_UpdateCompanyPinnedStatus
    @UserId INT,
    @CompanyId INT,
    @PinnedStatus BIT
AS
BEGIN
    SET NOCOUNT ON;
    IF @PinnedStatus = 1
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM dbo_professional360.Company WHERE CompanyId = @CompanyId)
            THROW 50001, N'company not found', 1;
        INSERT INTO dbo_professional360.UserPinnedCompanies (UserId, CompanyId, PinnedAt)
        SELECT @UserId, @CompanyId, GETUTCDATE()
        WHERE NOT EXISTS (SELECT 1 FROM dbo_professional360.UserPinnedCompanies WITH (UPDLOCK, HOLDLOCK)
                          WHERE UserId = @UserId AND CompanyId = @CompanyId);
    END
    ELSE
        DELETE FROM dbo_professional360.UserPinnedCompanies
        WHERE UserId = @UserId AND CompanyId = @CompanyId;
END;
GO

-- PUT api/companies/{company_id}/followUps/{follow_up_id}/flagged/{bool}. Flagging needs a current
-- question of this company: one replaced by a newer version (isActive = 0) answers 50002, and
-- the front end reloads the list. Unflagging always works (also for a replaced question).
CREATE OR ALTER PROCEDURE dbo_professional360.sp_UpdateCompanyFollowupQuestionFlag
    @CompanyId INT,
    @UserId INT,
    @followupId INT,
    @FlagStatus BIT
AS
BEGIN
    SET NOCOUNT ON;
    IF @FlagStatus = 1
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM dbo_professional360.FollowUpQuestions
                       WHERE FollowUpQuestionId = @followupId AND CompanyId = @CompanyId
                         AND isActive = 1)
            THROW 50002, N'follow-up question not current (replaced or removed): reload the list', 1;
        INSERT INTO dbo_professional360.UserFlaggedFollowupQuestions (UserId, FollowUpQuestionId, FlaggedAt)
        SELECT @UserId, @followupId, GETUTCDATE()
        WHERE NOT EXISTS (SELECT 1 FROM dbo_professional360.UserFlaggedFollowupQuestions WITH (UPDLOCK, HOLDLOCK)
                          WHERE UserId = @UserId AND FollowUpQuestionId = @followupId);
    END
    ELSE
        DELETE F
        FROM dbo_professional360.UserFlaggedFollowupQuestions F
        JOIN dbo_professional360.FollowUpQuestions Q ON Q.FollowUpQuestionId = F.FollowUpQuestionId
        WHERE F.UserId = @UserId AND F.FollowUpQuestionId = @followupId AND Q.CompanyId = @CompanyId;
END;
GO

-- PUT api/companies/{company_id}/categories/{categories_id}/metrics/selections, body
-- {"metricIds":[..]}: replaces the user's choice in that category, for the company's industry
-- (UserMetricsSelection is per SIC). @MetricIds is the JSON array, e.g. N'[1,2,3]'.
-- The first change in an industry starts from the industry default (SIC_Preferences), so the
-- other categories keep what the user saw. An empty list empties the category; when every
-- category is empty the user is back on the default (the table has no "chose nothing" state,
-- review item C2).
CREATE OR ALTER PROCEDURE dbo_professional360.sp_UpdateUserMetricSelection
    @UserId INT,
    @CompanyId INT,
    @CategoryId INT,
    @MetricIds NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @SicId INT = (SELECT SIC_Id FROM dbo_professional360.Company WHERE CompanyId = @CompanyId);
    IF @SicId IS NULL
        THROW 50001, N'company not found (or no industry)', 1;
    IF NOT EXISTS (SELECT 1 FROM dbo_professional360.metric_category
                   WHERE metric_category_Id = @CategoryId)
        THROW 50003, N'metric category not found', 1;
    IF ISJSON(ISNULL(@MetricIds, N'[]')) = 0
        THROW 50004, N'metricIds must be a JSON array of metric ids', 1;

    DECLARE @New TABLE (metric_id INT PRIMARY KEY);
    INSERT INTO @New (metric_id)
    SELECT DISTINCT TRY_CAST(j.value AS INT) FROM OPENJSON(ISNULL(@MetricIds, N'[]')) j
    WHERE TRY_CAST(j.value AS INT) IS NOT NULL;
    IF EXISTS (SELECT 1 FROM @New N
               LEFT JOIN dbo_professional360.metrics M
                 ON M.metric_id = N.metric_id AND M.metric_category_Id = @CategoryId
               WHERE M.metric_id IS NULL)
        THROW 50004, N'a metric is not in this category', 1;

    BEGIN TRANSACTION;
    IF NOT EXISTS (SELECT 1 FROM dbo_professional360.UserMetricsSelection WITH (UPDLOCK, HOLDLOCK)
                   WHERE UserId = @UserId AND SIC_Id = @SicId)
        INSERT INTO dbo_professional360.UserMetricsSelection (UserId, SIC_Id, metric_id)
        SELECT DISTINCT @UserId, @SicId, metric_id
        FROM dbo_professional360.SIC_Preferences
        WHERE SIC_Id = @SicId;

    DELETE S
    FROM dbo_professional360.UserMetricsSelection S
    JOIN dbo_professional360.metrics M ON M.metric_id = S.metric_id
    WHERE S.UserId = @UserId AND S.SIC_Id = @SicId AND M.metric_category_Id = @CategoryId;

    INSERT INTO dbo_professional360.UserMetricsSelection (UserId, SIC_Id, metric_id)
    SELECT @UserId, @SicId, metric_id FROM @New;
    COMMIT TRANSACTION;
END;
GO

-- PUT api/notifications/{key}/read : one notification read (clicked). Safe to repeat.
CREATE OR ALTER PROCEDURE dbo_professional360.sp_MarkNotificationRead
    @UserId INT,
    @NotificationKey NVARCHAR(100)
AS
BEGIN
    SET NOCOUNT ON;
    IF @NotificationKey NOT LIKE N'NEWS:%' AND @NotificationKey NOT LIKE N'INSIGHT:%'
       AND @NotificationKey NOT LIKE N'SIGNAL:%' AND @NotificationKey NOT LIKE N'FILING:%'
        THROW 50005, N'not a notification key', 1;
    INSERT INTO dbo_professional360.UserNotificationRead (UserId, NotificationKey, ReadAt)
    SELECT @UserId, @NotificationKey, GETUTCDATE()
    WHERE NOT EXISTS (SELECT 1 FROM dbo_professional360.UserNotificationRead WITH (UPDLOCK, HOLDLOCK)
                      WHERE UserId = @UserId AND NotificationKey = @NotificationKey);
END;
GO

-- PUT api/notifications/read : "mark all as read". Same arguments as udf_GetUserNotifications:
-- every notification of that list becomes read. Read rows over a year old are removed (their
-- events are long out of any window).
CREATE OR ALTER PROCEDURE dbo_professional360.sp_MarkAllNotificationsRead
    @UserId INT,
    @Days INT,
    @MinNewsRisk FLOAT
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo_professional360.UserNotificationRead (UserId, NotificationKey, ReadAt)
    SELECT @UserId, n.notificationKey, GETUTCDATE()
    FROM dbo_professional360.udf_GetUserNotifications(@UserId, @Days, @MinNewsRisk) n
    WHERE n.isRead = 0
      AND NOT EXISTS (SELECT 1 FROM dbo_professional360.UserNotificationRead R WITH (UPDLOCK, HOLDLOCK)
                      WHERE R.UserId = @UserId AND R.NotificationKey = n.notificationKey);
    DELETE FROM dbo_professional360.UserNotificationRead
    WHERE UserId = @UserId AND ReadAt < DATEADD(DAY, -365, GETUTCDATE());
END;
GO
