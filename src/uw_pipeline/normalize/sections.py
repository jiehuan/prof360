"""Split 10-K / 10-Q / 8-K text into items, and chunk sections for LLM citation.

Item headings are lines that start with "Item <n>". Consecutive headings of the same item
(page headers like "Item 7. ... (Continued)") are merged into one block. A filing lists each
item at least twice, once in the table of contents and once at the real section. For each
item the longest block wins; TOC entries are followed almost immediately by the next entry.

Section codes:
  10-K: "10K-1A", "10K-7", ...; also "10K-AUDIT" (auditor's report).
  10-Q: "10Q-I-2" (Part I Item 2 MD&A), "10Q-II-1A", ...
  8-K:  "8K-5.02", ...
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_ITEM_LINE = re.compile(r"^item\s*(\d{1,2}[a-c]?)\s*(?:[.:\-\u2013\u2014]|\s|$)", re.I)
_PART_LINE = re.compile(r"^part\s+(iv|i{1,3})\b", re.I)
_MAX_HEADING_LEN = 250
_8K_ITEM = re.compile(r"^item\s*(\d\.\d{2})\b", re.I)
_8K_END = re.compile(r"^(signatures?|item\s*9\.01)\b", re.I)
_AUDIT_HEADING = re.compile(r"report of independent registered public accounting firm", re.I)
_AUDITOR_SINCE = re.compile(
    r"serv(?:ed|ing) as the [^.]{0,120}?auditors? (?:continuously )?since (?:at least )?(\d{4})",
    re.I,
)


@dataclass(frozen=True)
class Section:
    code: str
    start: int  # offsets into the full document text
    end: int
    text: str


def _lines(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def split_items(text: str, form: str) -> list[Section]:
    """Item sections of a 10-K or 10-Q, in document order."""
    prefix = "10K" if form.startswith("10-K") else "10Q"
    with_parts = prefix == "10Q"
    candidates: list[tuple[int, str]] = []  # (offset, code)
    part = ""
    for offset, line in _lines(text):
        if len(line) > _MAX_HEADING_LEN:
            continue
        m = _PART_LINE.match(line)
        if m:
            part = m.group(1).upper()
            continue
        m = _ITEM_LINE.match(line)
        if m:
            item = m.group(1).upper()
            code = f"{prefix}-{part}-{item}" if with_parts and part else f"{prefix}-{item}"
            candidates.append((offset, code))
    if not candidates:
        return []

    # Collapse runs of the same item: many filings repeat "Item 7. ... (Continued)" as a page
    # header, so a section is everything from the first heading of a run to the next item.
    blocks: list[tuple[str, int, int]] = []  # (code, start, end)
    for offset, code in candidates:
        if blocks and blocks[-1][0] == code:
            continue
        if blocks:
            blocks[-1] = (blocks[-1][0], blocks[-1][1], offset)
        blocks.append((code, offset, len(text)))
    best: dict[str, tuple[int, int]] = {}
    for code, start, end in blocks:
        if code not in best or end - start > best[code][1] - best[code][0]:
            best[code] = (start, end)
    starts = sorted((start, code) for code, (start, _) in best.items())
    sections = []
    for i, (start, code) in enumerate(starts):
        # A chosen section runs to the next chosen heading, so cross-references that look like
        # headings inside it do not truncate it.
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        sections.append(Section(code, start, end, text[start:end].strip()))
    return sections


def section_by_headings(text: str, code: str, start: str, end: str) -> Section | None:
    """From the first line matching `start` to the first later line matching `end`."""
    start_re = re.compile(start, re.I | re.M)
    end_re = re.compile(end, re.I | re.M)
    for m in start_re.finditer(text):
        stop = end_re.search(text, m.end())
        if stop:
            return Section(code, m.start(), stop.start(), text[m.start() : stop.start()].strip())
    return None


def apply_rules(
    text: str, sections: list[Section], rules: Mapping[str, tuple[str, str]]
) -> list[Section]:
    """Replace (or add) sections located by configured heading rules."""
    by_code = {s.code: s for s in sections}
    for code, (start, end) in rules.items():
        found = section_by_headings(text, code, start, end)
        if found is not None:
            by_code[code] = found
    return sorted(by_code.values(), key=lambda s: s.start)


def audit_report(text: str) -> Section | None:
    """The auditor's report that states the auditor's tenure ("served as ... since YYYY")."""
    for m in _AUDIT_HEADING.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if (line_end if line_end != -1 else len(text)) - line_start > _MAX_HEADING_LEN:
            continue  # a mention in running text, not a heading
        window = text[line_start : line_start + 30_000]
        since = _AUDITOR_SINCE.search(window)
        if since:
            # Keep the sign-off lines (firm name, city, date) after the tenure sentence.
            # End of the tenure line, then three more lines (firm, city, date).
            end = line_start + since.end()
            for _ in range(4):
                nxt = text.find("\n", end + 1)
                end = nxt if nxt != -1 else len(text)
            return Section("10K-AUDIT", line_start, end, text[line_start:end].strip())
    return None


def auditor_since(section_text: str) -> int | None:
    m = _AUDITOR_SINCE.search(section_text)
    return int(m.group(1)) if m else None


def split_8k_items(text: str) -> list[Section]:
    """Item sections of an 8-K (e.g. 8K-5.02), excluding 9.01 exhibits and signatures."""
    marks: list[tuple[int, str | None]] = []
    for offset, line in _lines(text):
        if len(line) > _MAX_HEADING_LEN:
            continue
        if _8K_END.match(line):
            marks.append((offset, None))
            continue
        m = _8K_ITEM.match(line)
        if m:
            marks.append((offset, m.group(1)))
    sections = []
    for i, (start, item) in enumerate(marks):
        if item is None:
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        sections.append(Section(f"8K-{item}", start, end, text[start:end].strip()))
    return sections


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int  # offsets relative to the section text
    end: int
    text: str


def chunk(text: str, max_chars: int = 4000) -> list[Chunk]:
    """Split on line (paragraph) boundaries into chunks of at most ~max_chars."""
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start:
                end = cut
        chunks.append(Chunk(len(chunks), start, end, text[start:end]))
        start = end + 1 if end < len(text) and text[end] == "\n" else end
    return chunks
