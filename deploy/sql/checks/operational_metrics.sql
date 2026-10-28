-- Operational metrics per company (2026-10-29): what the database holds and what the company
-- page is told to show, to find why a company's operational cards look wrong (e.g. Marriott
-- without RevPAR). Read-only. One row per company, by SIC code.
--   has_data              operational metrics with values in MatrixData for the company
--   sic_default           the industry default (SIC_Preferences) for the company's SIC code
--   your_choice           the user's saved choice for that SIC code; when set, it replaces the
--                         default on every company with that SIC code
--   userMetricSelections  what udf_GetCompanyDetails returns to the page (all categories)
-- @UserId: the user whose page is checked. Run with scripts/databricks/run_query (widget
-- user_id declares it); elsewhere add "DECLARE @UserId INT = <id>;" first.
-- Expected from the 2026-10-28 data: MAR RevPAR / Occupancy Rate / Average Daily Rate, F Vehicle
-- Deliveries / Product Warranty Reserve, T postpaid churn / net additions, UNH medical cost
-- ratio / members, XOM production, AAPL R&D / Revenue.
SELECT
    C.NameAbbreviation AS ticker,
    S.SIC_Code AS sicCode,
    (SELECT STRING_AGG(M.metric_name, ', ') WITHIN GROUP (ORDER BY M.metric_id)
     FROM dbo_professional360.metrics M
     JOIN dbo_professional360.metric_category MC ON MC.metric_category_Id = M.metric_category_Id
     WHERE MC.categoryName = N'Operational Metrics'
       AND EXISTS (SELECT 1 FROM dbo_professional360.MatrixData D
                   WHERE D.CompanyId = C.CompanyId AND D.metric_id = M.metric_id)) AS has_data,
    (SELECT STRING_AGG(M.metric_name, ', ') WITHIN GROUP (ORDER BY M.metric_id)
     FROM dbo_professional360.SIC_Preferences P
     JOIN dbo_professional360.metrics M ON M.metric_id = P.metric_id
     JOIN dbo_professional360.metric_category MC ON MC.metric_category_Id = M.metric_category_Id
     WHERE P.SIC_Id = C.SIC_Id AND MC.categoryName = N'Operational Metrics') AS sic_default,
    (SELECT STRING_AGG(M.metric_name, ', ') WITHIN GROUP (ORDER BY M.metric_id)
     FROM dbo_professional360.UserMetricsSelection X
     JOIN dbo_professional360.metrics M ON M.metric_id = X.metric_id
     JOIN dbo_professional360.metric_category MC ON MC.metric_category_Id = M.metric_category_Id
     WHERE X.UserId = @UserId AND X.SIC_Id = C.SIC_Id
       AND MC.categoryName = N'Operational Metrics') AS your_choice,
    D.userMetricSelections
FROM dbo_professional360.Company C
LEFT JOIN dbo_professional360.SIC_Code S ON S.SIC_Id = C.SIC_Id
OUTER APPLY dbo_professional360.udf_GetCompanyDetails(@UserId, C.CompanyId) D
ORDER BY S.SIC_Code, C.NameAbbreviation;
