"""Check that LLM evidence quotes actually occur in the source text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_DASHES = re.compile(r"[\u2010-\u2015]")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = _DASHES.sub("-", text.translate(_QUOTES))
    return _WS.sub(" ", text).strip().lower()


_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
# Each piece of an elided quote ("Comparable Systemwide ... Worldwide $ 131.43") must be at
# least this long, so elision cannot stitch together arbitrary fragments.
MIN_PIECE = 8


# Footnote markers glued to a word ("capital3", "estate(a)"); models often leave them out
# when quoting, so a second comparison ignores them on both sides.
# Digits count as a footnote only after a word of 3+ letters ("capital3"), so period labels such
# as "q3" or "fy24" are never stripped (they would become interchangeable).
_FOOTNOTE = re.compile(r"(?:(?<=[a-z]{3})\d{1,2}|(?<=[a-z)])\([a-z0-9]{1,2}\))+(?=[\s,.;:)]|$)")


def is_grounded(quote: str, source: str, *, min_chars: int = 12) -> bool:
    if _grounded(quote, source, min_chars):
        return True
    return _grounded(_FOOTNOTE.sub("", normalize(quote)), _FOOTNOTE.sub("", normalize(source)),
                     min_chars)  # fmt: skip


def _grounded(quote: str, source: str, min_chars: int) -> bool:
    """True if the quote (whitespace/quote-style/case-insensitive) appears in the source.

    A quote wrapped in quotation marks, and a leading or trailing "...", are tolerated. A quote
    elided with "..." in the middle is grounded when every piece occurs in the source, in that
    order (models often join a table header and a row this way)."""
    q = normalize(quote).strip("\"'").strip()
    pieces = [x for x in _ELLIPSIS.split(q) if x]
    src = normalize(source)
    if len(pieces) == 1:
        return len(pieces[0]) >= min_chars and pieces[0] in src
    if sum(len(x) for x in pieces) < min_chars or any(len(x) < MIN_PIECE for x in pieces):
        return False
    at = 0
    for piece in pieces:
        found = src.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    return True


# -- numbers in generated text (PLAN §7.1) -----------------------------------------------------

_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-\u2212])?\$?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s*(?P<unit>%|x\b|bps\b|basis points|billion\b|bn\b|B\b|million\b|mn\b|M\b|trillion\b|T\b)?",
    re.I,
)
_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6,
          "trillion": 1e12, "t": 1e12}  # fmt: skip


@dataclass(frozen=True)
class NumberMention:
    text: str
    literal: str  # the digits as written, without separators, e.g. "1819" or "4.9"
    value: float
    decimals: int
    unit: str  # "", "%", "x", "bps", or a scale word


def numbers_in(text: str) -> list[NumberMention]:
    """Financial numbers in generated text; years, quarter numbers and small counts are skipped."""
    out = []
    for m in _NUMBER.finditer(text):
        digits = m.group("num").replace(",", "")
        dec = m.group("dec") or ""
        unit = (m.group("unit") or "").lower()
        value = float(digits + dec)
        has_money = m.group(0).lstrip("-\u2212").startswith("$")
        prefix = text[max(0, m.start() - 2) : m.start()]
        if not unit and not dec and not has_money:
            if 1900 <= value <= 2100 or value < 100:
                continue  # years, days of month, quarters, item numbers, counts
            if prefix.endswith(("Q", "FY", "H")):
                continue
        if prefix.endswith("Q") or prefix.endswith("FY"):
            continue
        out.append(
            NumberMention(m.group(0).strip(), digits + dec, value, len(dec) - 1 if dec else 0, unit)
        )
    return out


def _matches_value(n: NumberMention, actual: float) -> bool:
    """Does the mention express `actual` (stored in base units / fractions), allowing for unit
    conversion and rounding to the mention's precision?"""
    tolerance = 0.5 * 10 ** (-n.decimals) if n.decimals else 0.5
    scales: list[float]
    if n.unit == "%":
        scales = [100.0]
    elif n.unit == "bps" or n.unit == "basis points":
        scales = [10_000.0]
    elif n.unit in _SCALE:
        scales = [1 / _SCALE[n.unit]]
    else:
        scales = [1.0, 1e-6, 1e-9, 100.0]
    return any(abs(abs(actual) * s - n.value) <= tolerance + 1e-9 for s in scales)


def ungrounded_numbers(text: str, quotes: Iterable[str], values: Iterable[float]) -> list[str]:
    """Numbers in `text` found neither in the quoted source text nor among `values`."""
    quote_text = normalize(" ".join(quotes)).replace(",", "")
    vals = list(values)
    missing = []
    for n in numbers_in(text):
        # The number as written appears in a cited source ("$27M" vs "$27 million" both
        # contain 27), bounded so that 27 does not match inside 2027, 1.27 or 27.5.
        if re.search(rf"(?<![\d.]){re.escape(n.literal)}(?!\.?\d)", quote_text):
            continue
        if any(_matches_value(n, v) for v in vals):
            continue
        missing.append(n.text)
    return missing
