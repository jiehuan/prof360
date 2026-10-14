"""Read metrics from filing tables by cell coordinates and check each answer (decisions D-044).

The model sees the tables as grids (`normalize.table_grid`) and answers with a table, row and
column. `check_cell` then accepts the answer only if, mechanically:
- the cell holds that number;
- the column's header names the metric (`TableSpec.column`) and is not a change column;
- the period matches: the lowest header line in that column with a year has exactly that one
  year, the month of the period end appears in the header, and the header's own words for the
  period length (three months / year ended / ...) agree with the claimed length;
- the row's label, the group heading above it or the table title carries each scope group.
A rejected answer is never repaired by looking at other cells: the value is withheld. Accepted
answers become ordinary extracted values, so the unit, period-length and identity checks of the
text-metric stage still apply.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from uw_pipeline.compute.registry import TableSpec
from uw_pipeline.enrich.schemas import ExtractedValue, LoanCell, TableCell
from uw_pipeline.enrich.text_metrics import SCALES, LoanRow, loan_key, parse_number
from uw_pipeline.normalize.table_grid import Table

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]  # fmt: skip
_QUARTER = re.compile(r"three months|quarter|\bq[1-4]\b|13 weeks|thirteen weeks", re.I)
_YTD = re.compile(r"six months|nine months|year[- ]to[- ]date|\bytd\b|26 weeks|39 weeks", re.I)
_YEAR_WORDS = re.compile(r"year ended|years ended|twelve months|full[- ]year|fiscal year|"
                         r"52 weeks|53 weeks", re.I)  # fmt: skip


@dataclass(frozen=True)
class Checked:
    value: ExtractedValue | None  # None: withheld
    reason: str


def _title(table: Table) -> str:
    """Rows above the first numeric row whose cells all span the table (titles, captions)."""
    out = []
    for row in table.rows:
        if any(re.search(r"\d", c) for c in row) and len(set(row)) > 1:
            break
        if len(set(row)) == 1 and row[0]:
            out.append(row[0])
    return " ".join(out)


def _period_header(table: Table, row: int, col: int) -> tuple[str, str]:
    """(all header text above the cell, the lowest header line in the column with a year)."""
    lines = [table.rows[r][col] for r in range(row) if table.rows[r][col]]
    header = " ".join(dict.fromkeys(line for line in lines if not _is_data(line)))
    dated = [line for line in lines if _YEAR.search(line) and not _is_data(line)]
    return header, dated[-1] if dated else ""


def _own_header(table: Table, row: int, col: int) -> str:
    """Header lines above the cell that belong to its column alone: a heading spanning other
    data cells of the row (e.g. "Three Months Ended ... and Change vs. ...") is shared."""
    data_cols = {c for c in range(table.width) if c != col and _is_data(table.rows[row][c])}
    own = []
    for r in range(row):
        text = table.rows[r][col]
        if not text or _is_data(text):
            continue
        lo = hi = col
        while lo > 0 and table.rows[r][lo - 1] == text:
            lo -= 1
        while hi + 1 < table.width and table.rows[r][hi + 1] == text:
            hi += 1
        if not any(lo <= c <= hi for c in data_cols):
            own.append(text)
    return " ".join(dict.fromkeys(own))


def _is_data(text: str) -> bool:
    return parse_number(text) is not None and not _YEAR.fullmatch(text.strip())


def check_cell(tables: dict[str, Table], cell: TableCell, spec: TableSpec) -> Checked:
    table = tables.get(cell.table.strip().upper())
    if table is None:
        return Checked(None, f"no table {cell.table}")
    text = table.cell(cell.row, cell.col)
    if not text:
        return Checked(None, f"{cell.table} r{cell.row} c{cell.col} is empty or outside")
    shown, claimed = parse_number(text), parse_number(cell.value)
    if shown is None or claimed is None or abs(shown - claimed) > 1e-9 * max(1, abs(shown)):
        return Checked(None, f"cell holds {text!r}, not {cell.value!r}")
    header, dated = _period_header(table, cell.row, cell.col)
    low = header.lower()
    label, section = table.row_label(cell.row), table.section_label(cell.row)
    named = f"{low} {label.lower()}"  # Hilton puts the metric in the row, Marriott in the column
    if not any(term.lower() in named for term in spec.column):
        return Checked(None, f"neither header {header!r} nor row {label!r} names {spec.column}")
    own = _own_header(table, cell.row, cell.col).lower()
    if any(term.lower() in own for term in spec.exclude):
        return Checked(None, f"column header {own!r} is a change column")
    subset = f"{label} {section}".lower()
    hit = next((w for w in spec.exclude_rows if w.lower() in subset), None)
    if hit:
        return Checked(None, f"row {label!r} under {section!r} is a subset ({hit})")
    stub, _ = _period_header(table, cell.row, 0)  # the top-left header ("year ended ...")
    if not dated and _YEAR.fullmatch(table.section_label(cell.row).strip()):
        dated = table.section_label(cell.row)  # years as row groups, measures across
    problem = _period_problem(cell, header, dated, f"{_title(table)} {stub} {header}")
    if problem:
        return Checked(None, problem)
    context = f"{_title(table)} | {section} | {label}"
    unreported = next((w for w in spec.exclude_tables if w in context.lower()), None)
    if unreported:
        return Checked(None, f"table is not reported figures ({unreported})")
    for group in spec.scope:
        if not any(word.lower() in context.lower() for word in group):
            return Checked(None, f"row context {context!r} lacks any of {group}")
    quote = f"{label} | {header} | {text}"[:300]
    return Checked(
        ExtractedValue(
            metric_code=cell.metric_code, value=cell.value, unit=cell.unit, scale=cell.scale,
            period_start=cell.period_start, period_end=cell.period_end, period=cell.period,
            evidence=quote,
        ),
        "ok",
    )  # fmt: skip


def _period_problem(cell: TableCell, header: str, dated: str, context: str) -> str | None:
    try:
        end = date.fromisoformat(cell.period_end)
    except ValueError:
        return f"bad period end {cell.period_end!r}"
    years = {m.group(0) for m in _YEAR.finditer(dated)}
    if years != {str(end.year)}:
        return f"column's dated header {dated!r} is not only {end.year}"
    month = _MONTHS[end.month - 1]
    # Header or title; "jun" also matches "june". Annual tables often show only the year.
    if cell.period != "year" and month[:3] not in context.lower():
        return f"header {header!r} does not show {month.title()}"
    if cell.period == "year_to_date" or _YTD.search(context):
        return "year-to-date column"
    if cell.period == "quarter" and not _QUARTER.search(context):
        return "header does not say the column is a quarter"
    if cell.period == "year" and not _YEAR_WORDS.search(context):
        return "header does not say the column is a year"
    if cell.period == "point_in_time" and (_QUARTER.search(context) or _YEAR_WORDS.search(context)):
        return "a duration column read as a point in time"
    return None


def could_pass(table: Table, spec: TableSpec) -> bool:
    """The table has a row a cell answer could pass on: it names the metric (`find` or
    `column`) and is not a subset row (`exclude_rows`). Tables that only hold subsets (a
    segment's "Total international net revenue") are not sent at all."""
    terms = [t.lower() for t in (spec.find or spec.column)]
    for r in range(len(table.rows)):
        label = f"{table.row_label(r)} {table.section_label(r)}".lower()
        cells = " ".join(table.rows[r]).lower()
        if not any(t in cells or t in label for t in terms):
            continue
        if not any(w.lower() in label for w in spec.exclude_rows):
            return True
    flat = " ".join(" ".join(row) for row in table.rows[:6]).lower()  # metric named in a header
    return not spec.exclude_rows and any(t in flat for t in terms)


def relevant(tables: Sequence[Table], terms: Sequence[str], max_chars: int) -> list[Table]:
    """Tables naming any of `terms`, most mentions first, within the prompt budget."""
    scored = []
    for t in tables:
        flat = " ".join(" ".join(r) for r in t.rows).lower()
        hits = sum(flat.count(term.lower()) for term in terms)
        if hits:
            scored.append((-hits, t.index, t))
    out, used = [], 0
    for _, _, t in sorted(scored, key=lambda x: (x[0], x[1])):
        size = len(t.render())
        if used + size > max_chars:
            continue
        out.append(t)
        used += size
    return sorted(out, key=lambda t: t.index)


# Loan book (D-044): which column headers hold balances and which hold nonaccrual loans.
BALANCE_HEADERS = ("credit exposure", "loans", "retained", "outstanding", "balance")
NOT_BALANCE = ("nonaccrual", "nonperforming", "allowance", "charge-off", "charge-offs",
               "delinquen", "past due", "commitments", "change")  # fmt: skip
NONACCRUAL_HEADERS = ("nonaccrual", "nonperforming")


def check_loan(tables: dict[str, Table], cell: LoanCell) -> tuple[LoanRow | None, str]:
    """A loan class checked by cell, in either layout: classes as rows (the class in the row's
    label or group heading, nonaccrual in another column) or classes as columns (the class in
    the column header, the balance in a loans row, nonaccrual in a nonaccrual row)."""
    table = tables.get(cell.table.strip().upper())
    if table is None or not 0 <= cell.row < len(table.rows):
        return None, f"no row {cell.table} r{cell.row}"
    label, section = table.row_label(cell.row), table.section_label(cell.row)
    segment = set(loan_key(cell.segment).split())
    heading = table.section_label(cell.row, first_cell=True)  # not the column headings
    by_row = segment <= set(loan_key(f"{label} {heading} {_title(table)}").split())
    own_header = _period_header(table, cell.row, cell.outstanding_col)[0]
    by_col = not by_row and segment <= set(loan_key(own_header).split()) and \
        "loans" in label.lower()  # fmt: skip
    if not (by_row or by_col):
        return (
            None,
            f"neither row {label!r} under {section!r} nor its column names {cell.segment!r}",
        )
    if by_row and "total" in loan_key(label).split():
        return None, f"row {label!r} is a total"
    targets: list[tuple[int, int, tuple[str, ...], tuple[str, ...]] | None] = [
        (cell.row, cell.outstanding_col, BALANCE_HEADERS, NOT_BALANCE)
    ]
    if by_row:
        targets.append(None if cell.nonaccrual_col is None else
                       (cell.row, cell.nonaccrual_col, NONACCRUAL_HEADERS, ()))  # fmt: skip
    else:
        na_row = cell.nonaccrual_row
        targets.append(None if na_row is None else
                       (na_row, cell.outstanding_col, NONACCRUAL_HEADERS, ()))  # fmt: skip
    values: list[float | None] = []
    for target in targets:
        if target is None:
            values.append(None)
            continue
        row, col, wanted, banned = target
        text = table.cell(row, col)
        number = parse_number(text or "")
        header, dated = _period_header(table, row, col)
        low = header.lower()
        named = f"{low} {table.row_label(row).lower()}" if by_col else low
        if number is None:
            return None, f"r{row} c{col} holds {text!r}"
        if not any(w in named for w in wanted) or any(w in low for w in banned):
            return None, f"column header {header!r} is not {wanted[0]}"
        try:
            end = date.fromisoformat(cell.period_end)
        except ValueError:
            return None, f"bad period end {cell.period_end!r}"
        if {m.group(0) for m in _YEAR.finditer(dated)} != {str(end.year)}:
            return None, f"column's dated header {dated!r} is not only {end.year}"
        if _MONTHS[end.month - 1][:3] not in low:
            return None, f"header {header!r} does not show the month of {cell.period_end}"
        values.append(number * SCALES[cell.scale])
    outstanding, nonaccrual = values
    assert outstanding is not None
    quote = f"{label} | {table.rows[cell.row][cell.outstanding_col]}"
    return LoanRow(segment=cell.segment.strip(), parent_segment=cell.parent_segment.strip(),
                   period_end=cell.period_end, outstanding=outstanding, nonaccrual=nonaccrual,
                   evidence=quote[:300]), "ok"  # fmt: skip
