-- Metric category "Operational Metrics" renamed "Operational" (2026-10-29): the company page
-- titles each section "<name> Metrics", which showed "Operational Metrics Metrics". The id (5)
-- and every metric in it stay; only the name changes, as config/app_publish.yaml now publishes
-- it. Safe to run more than once. Schema [dbo_professional360]; if the tables are in [dbo],
-- replace "dbo_professional360." with "dbo.".
UPDATE dbo_professional360.metric_category
SET categoryName = N'Operational'
WHERE categoryName = N'Operational Metrics';
GO
