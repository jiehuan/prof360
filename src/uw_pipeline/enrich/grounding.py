"""Check that LLM evidence quotes actually occur in the source text."""

from __future__ import annotations

import re

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_DASHES = re.compile(r"[\u2010-\u2015]")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = _DASHES.sub("-", text.translate(_QUOTES))
    return _WS.sub(" ", text).strip().lower()


def is_grounded(quote: str, source: str, *, min_chars: int = 12) -> bool:
    """True if the quote (whitespace/quote-style/case-insensitive) appears in the source.

    A trailing "..." or a quote wrapped in quotation marks is tolerated."""
    q = normalize(quote).strip("\"'").removesuffix("...").removesuffix("…").strip()
    return len(q) >= min_chars and q in normalize(source)
