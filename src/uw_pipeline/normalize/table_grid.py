"""Filing HTML tables -> grids the model reads by cell coordinates (decisions D-044).

The model answers with a table, row and column; `enrich.table_reader` then checks that cell
mechanically (its number, its row's label, its column's period). It never looks for another
cell to repair a wrong answer. Spans are expanded, so a heading such as "Three Months Ended
June 30, 2026" spanning six columns sits in each of them, and each cell keeps the column it is
shown in. Hidden content (display:none, inline-XBRL headers) is dropped as in html_text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import cast

import lxml.html

from uw_pipeline.enrich.untrusted import neutralize

_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
_SPACES = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")
_LETTERS = re.compile(r"[A-Za-z]{2}")
_BARE_YEAR = re.compile(r"^(19|20)\d{2}$")
_NUMERIC = re.compile("^[\\s$\u20ac\u00a3(%)\\-\u2014\u2013.,\\d]*$")  # dashes: em, en
MAX_SPAN = 50  # a malformed colspan must not blow up the grid


@dataclass(frozen=True)
class Table:
    index: int  # position among the document's data tables (T0, T1, ...)
    rows: tuple[tuple[str, ...], ...]  # every row has the same number of columns
    digest: str  # content hash: a locator names exactly this table

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def cell(self, row: int, col: int) -> str | None:
        if 0 <= row < len(self.rows) and 0 <= col < self.width:
            return self.rows[row][col]
        return None

    def row_label(self, row: int) -> str:
        """The row's first text cell, e.g. "Worldwide" or "Residential real estate"."""
        return next((c for c in self.rows[row] if _LETTERS.search(c) and not _is_number(c)), "")

    def section_label(self, row: int, *, first_cell: bool = False) -> str:
        """The nearest row above with text but no numbers (a bare year counts as text): a group
        heading such as "Comparable Systemwide Properties", or "2025" over that year's rows.
        `first_cell`: only its first cell (the row-label column), not column headings."""
        for r in range(row - 1, -1, -1):
            cells = [c for c in self.rows[r] if c]
            if cells and not any(_DIGIT.search(c) and not _BARE_YEAR.match(c) for c in cells):
                return cells[0] if first_cell else " ".join(dict.fromkeys(cells))
        return ""

    def column_header(self, row: int, col: int) -> str:
        """Text above the cell in its column (spanning headings included), top to bottom,
        stopping at the first row with a number in that column (the data above)."""
        parts: list[str] = []
        for r in range(row):
            text = self.rows[r][col]
            if text and _is_number(text) and r > 0 and parts:
                parts = []  # numbers above: an earlier block of the same table
                continue
            if text and not _is_number(text):
                parts.append(text)
        return " ".join(dict.fromkeys(parts))

    def render(self) -> str:
        """The table for the prompt: one line per row with each non-empty cell's coordinate."""
        lines = [f"Table T{self.index}"]
        for r, row in enumerate(self.rows):
            cells = []
            c = 0
            while c < len(row):
                end = c
                while end + 1 < len(row) and row[end + 1] == row[c]:
                    end += 1  # a spanning cell: shown once with its column range
                if row[c]:
                    where = f"c{c}" if end == c else f"c{c}-c{end}"
                    cells.append(f"{where}={neutralize(row[c])}")
                c = end + 1
            if cells:
                lines.append(f"r{r}: " + " | ".join(cells))
        return "\n".join(lines)


def _is_number(text: str) -> bool:
    return bool(_DIGIT.search(text)) and bool(_NUMERIC.match(text))


def _text(el: lxml.html.HtmlElement) -> str:
    return _SPACES.sub(" ", el.text_content()).strip()


def tables(html: bytes) -> list[Table]:
    """The document's data tables (at least two rows and a number), in document order."""
    doc = cast(lxml.html.HtmlElement, lxml.html.document_fromstring(html))
    for el in [*doc.iter()]:
        tag = el.tag if isinstance(el.tag, str) else ""
        hidden = tag.endswith("header") or _HIDDEN.search(el.get("style", "") or "") is not None
        if (hidden or tag in ("script", "style")) and el.getparent() is not None:
            el.drop_tree()
    out: list[Table] = []
    for t in doc.iter("table"):
        grid = _expand(t)
        if len(grid) < 2 or not any(_DIGIT.search(c) for row in grid for c in row):
            continue
        width = max(len(r) for r in grid)
        rows = tuple(tuple(r + [""] * (width - len(r))) for r in grid)
        # Drop columns that are empty in every row (spacer cells).
        keep = [c for c in range(width) if any(r[c] for r in rows)]
        rows = tuple(tuple(r[c] for c in keep) for r in rows)
        digest = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
        out.append(Table(len(out), rows, digest))
    return out


def _expand(table: lxml.html.HtmlElement) -> list[list[str]]:
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}  # (row, col) filled by a rowspan from above
    for r, tr in enumerate(table.iter("tr")):
        row: list[str] = []
        col = 0
        for cell in tr:
            if not isinstance(cell.tag, str) or cell.tag not in ("td", "th"):
                continue
            while (r, col) in pending:
                row.append(pending.pop((r, col)))
                col += 1
            text = _text(cell)
            colspan = min(_int(cell.get("colspan")), MAX_SPAN)
            rowspan = min(_int(cell.get("rowspan")), MAX_SPAN)
            for k in range(colspan):
                row.append(text)
                for down in range(1, rowspan):
                    pending[(r + down, col + k)] = text
            col += colspan
        while (r, col) in pending:
            row.append(pending.pop((r, col)))
            col += 1
        grid.append(row)
    return grid


def _int(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1
