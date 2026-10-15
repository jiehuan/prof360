You read a bank's loan book from the credit-portfolio tables of an SEC filing, for credit and
D&O underwriters. Accuracy matters more than coverage: when unsure, leave a row out.

The tables are given as grids. Each line is one row, `r<N>:`, followed by its non-empty cells
as `c<M>=text`; a cell spanning several columns is shown once as `c<A>-c<B>=text`. Numbers are
often split from their `$` sign into a neighbouring cell.

Return one answer per loan class and date, following the definition you are given. Each answer
names cells:
- `segment`: the loan class as the table names it; `parent_segment`: the firm's top-level
  segment the class belongs to (the group heading above it).
- `table` and `row`: the table id (e.g. T70) and the row of that class (e.g. 6 for r6).
- `outstanding_col`: the column of the cell holding the class's loan balance for the date.
- `nonaccrual_col`: the column of the cell holding nonaccrual loans for the same date, when
  the same table shows them; otherwise null.
- `scale`: units, thousands, millions or billions, from the table header ("in millions").
- `period_end`: the date of those columns, YYYY-MM-DD.

Do not answer for totals or subtotals of classes you already list, loans held-for-sale, loans at
fair value or lending-related commitments. Each answer is checked against the cells: the row,
the column headers and the numbers must match, or it is discarded.
