-- Loan portfolio credit quality for the BFF (2026-10-26), to add on its own. The same
-- definitions are in bff_functions.sql; algorithm and bands: docs/frontend-api-review.md (A3).
--
--   1. udf_GetLoanQuality(@NonaccrualRatio)       new: quality band of a nonaccrual ratio
--   2. udf_GetLoanQualityTrend(@Ratio, @Prior)     new: Improving / Stable / Deteriorating
--   3. udf_GetCompanyLoanQualitySummary(@CompanyId) new: the whole loan book, one row per period
--   4. udf_GetCompanyLoanPortfolios(@CompanyId)     updated: adds quality and qualityTrend
--      of each class and book* columns of the whole book, computed inside (2026-10-27): it
--      needs none of 1-3, so it can be run on its own and the endpoint keeps one function.
--
-- Schema [dbo_professional360]; if the tables are in [dbo], replace "dbo_professional360."
-- with "dbo." throughout. Run 1 and 2 before 3, which calls them.
-- Tested 2026-10-26 on SQL Server 2022 (JPMorgan's loan classes and book; band boundaries).

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

-- GET api/companies/{id}/loanPortfolio : one row per loan class and period (banks), with the
-- credit quality computed here: this one function is all the endpoint needs. periodRank 1 =
-- current period end, 2 = prior year end. The modal's methodology and sources:
-- udf_GetCompanyMetric with the "Loan Portfolio Composition" metric.
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
