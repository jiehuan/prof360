-- Notifications (the bell on the home page) for the BFF, 2026-10-27, to add on its own; the
-- functions are also in bff_functions.sql and the procedures in bff_procedures.sql. Reads the
-- pipeline's tables and the user's favorites and pins; the read state is a new user table,
-- UserNotificationRead (created below if missing). Schema [dbo_professional360]; if the tables
-- are in [dbo], replace "dbo_professional360." with "dbo." throughout. Run in this order.
--
--   GET api/notifications             udf_GetUserNotifications(@UserId, @Days, @MinNewsRisk)
--   GET api/notifications/count       udf_GetUserNotificationCount(same arguments): red dot
--   PUT api/notifications/{key}/read  sp_MarkNotificationRead(@UserId, @NotificationKey)
--   PUT api/notifications/read        sp_MarkAllNotificationsRead(same arguments as the list)
--
-- Tested 2026-10-27 on SQL Server 2022 (Marriott, Pfizer, JPMorgan; a test user with a favorite
-- and pins): all four event types, a company both favorite and pinned listed once, another user
-- gets nothing, @Days and @MinNewsRisk applied; 31 unread, one read (twice) -> 30, mark all
-- (twice) -> 0 with 31 read rows; an unknown key is refused (50005).

-- Read state of the bell's notifications: one row per user and notification read. A user table
-- (owned by the BFF, never written by the pipeline; the copy notebook skips User* tables).
-- NotificationKey: NEWS:<ActivityId>, INSIGHT:<KeyInsights.UniqueID>,
-- SIGNAL:<StrategicRiskId> or FILING:<SEC accession number>.
IF OBJECT_ID(N'dbo_professional360.UserNotificationRead', N'U') IS NULL
CREATE TABLE dbo_professional360.UserNotificationRead (
    UserId INT NOT NULL,
    NotificationKey NVARCHAR(100) NOT NULL,
    ReadAt DATETIME NOT NULL,
    PRIMARY KEY (UserId, NotificationKey)
);
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
