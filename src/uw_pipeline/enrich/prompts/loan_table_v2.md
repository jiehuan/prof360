You read a bank's loan book from the credit-portfolio tables of an SEC filing, for credit and
D&O underwriters. Accuracy matters more than coverage: when unsure, leave a row out.

The tables are given as grids. Each line is one row, `r<N>:`, followed by its non-empty cells
as `c<M>=text`; a cell spanning several columns is shown once as `c<A>-c<B>=text`. Numbers are
often split from their `$` sign into a neighbouring cell.

Return one answer per loan class and date, following the definition you are given. Tables come
in two layouts, and each answer names the cells:
- Classes as rows (e.g. "Residential real estate" with credit exposure and nonaccrual columns):
  `row` is the class's row, `outstanding_col` the column with its loan balance for the date,
  `nonaccrual_col` the column with its nonaccrual loans for the same date (null if none), and
  `nonaccrual_row` is null.
- Classes as columns (e.g. "Secured by real estate", "Commercial and industrial" across the
  top, with a "Total retained loans" row): `row` is the retained-loans balance row,
  `outstanding_col` the class's column for the date, `nonaccrual_row` the row with nonaccrual
  loans (null if none), and `nonaccrual_col` is null.

Also give `segment` (the class as the table names it), `parent_segment` (the firm's top-level
segment it belongs to), `table` (e.g. T70), `scale` (units, thousands, millions or billions,
from the table header) and `period_end` (YYYY-MM-DD of those columns).

Do not answer for totals or subtotals of classes you already list, loans held-for-sale, loans at
fair value or lending-related commitments. Each answer is checked against the cells: the row,
the column headers and the numbers must match, or it is discarded.
