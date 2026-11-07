"""Grounding: every number in an answer must come from the data points it cites (D-091).

A number in the answer text is grounded when, read with its unit ($3.65B, 3,646 million,
22.2x, 4.9%), it equals within its own rounding (tried in this order):
  - a cited data point's value (a loss may be written without its minus sign), or
  - another data point's value: the answer forgot to cite it, and it is added to the
    citations (Check.added), or
  - a simple computation on cited values: a + b, a - b, a / b, the change (a - b) / |b|, or
    the sum of all cited values in one unit, or
  - a number the context's own text states: a data-quality finding's message, an N/M reason,
    the coverage note, a company override's reason or a proxy tag's description, or
  - a number from the user's question, repeated ("the 0.44x you mention"; Check.echoed): the
    user wrote it, and the answer is logged with the question.
Not counted as numbers: years (1990-2039), period labels (FY2025, Q3, 2026Q3), dates, data-point
ids, accession numbers, dates (January 27, 2026), document references (Exhibit 99.1, Item 2.02,
Note 12), whole numbers
up to 10 (counts such as "2 of 5"), and the peer count.
A failed check means the BFF shows CANNOT_CONFIRM instead of the answer.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .models import ChatAnswer, ChatContext, DataPoint

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
# Removed before numbers are read.
_NOT_NUMBERS = re.compile(
    r"dp:[\w.:-]+"  # data-point ids
    r"|\b\d{10}-\d{2}-\d{6}\b"  # accession numbers
    r"|\b\d{4}-\d{2}-\d{2}\b"  # dates
    r"|\bFY\s?\d{2,4}\b|\b\d{4}\s?Q[1-4]\b|\bQ[1-4](?:\s?(?:FY)?\s?\d{2,4})?\b"  # periods
    r"|\b[A-Za-z-]+:[A-Za-z]+\b"  # XBRL tags (us-gaap:InterestPaidNet)
    r"|\b(?:Exhibit|EX-|Item|Note|Section|Rule|Part|Schedule)\s?[\w.()-]*\d[\w.()-]*"
    rf"|\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?P<neg>[-\u2212\u2013](?=\s?[$\d]))?\s?(?P<cur>[$\u20ac\u00a3])?\s?(?P<neg2>[-\u2212])?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suf>\s?(?:%|percent\b|x\b|\u00d7|times\b|trillion\b|tn\b|T\b|billion\b|bn\b|B\b"
    r"|million\b|mn\b|mm\b|M\b|thousand\b|K\b|k\b|bps\b|basis points\b))?",
)
_SCALE = {
    "trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3,
}  # fmt: skip
_YEARS = range(1990, 2040)
REL_TOLERANCE = 0.006  # rounding by the model beyond the digits shown (0.6%)


@dataclass(frozen=True)
class Mention:
    text: str
    candidates: tuple[tuple[float, float], ...]  # (value, tolerance)


@dataclass
class Check:
    ok: bool
    reason: str = ""
    computed: list[str] = field(default_factory=list)  # numbers from a computation, explained
    added: list[str] = field(default_factory=list)  # data points used but not cited
    echoed: list[str] = field(default_factory=list)  # numbers repeated from the question
    unmatched: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        parts = [self.reason] if self.reason else []
        if self.computed:
            parts.append("computed: " + "; ".join(self.computed))
        if self.added:
            parts.append("not cited, added: " + ", ".join(self.added))
        if self.echoed:
            parts.append("repeats the question's " + ", ".join(self.echoed))
        return " | ".join(parts)


def mentions(text: str) -> list[Mention]:
    """The numbers of `text` that need grounding, each with the values it may stand for."""
    out: list[Mention] = []
    clean = _NOT_NUMBERS.sub(" ", text)
    for m in _NUMBER.finditer(clean):
        raw, suf = m.group("num"), (m.group("suf") or "").strip().lower()
        if re.match(r"\s?(?:st|nd|rd|th)\b", clean[m.end() :]):  # ordinals: "5th"
            continue
        value = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        negative = bool(m.group("neg") or m.group("neg2"))
        plain = not suf and not m.group("cur")
        if plain and "." not in raw and (value <= 10 or int(value) in _YEARS):
            continue
        step = 0.5 * 10.0**-decimals
        cands: list[tuple[float, float]] = []
        if suf in ("%", "percent"):
            cands = [(value / 100, step / 100), (value, step)]
        elif suf in ("bps", "basis points"):
            cands = [(value / 10_000, step / 10_000)]
        elif suf in ("x", "\u00d7", "times"):
            cands = [(value, step)]
        elif suf in _SCALE:
            cands = [(value * _SCALE[suf], step * _SCALE[suf])]
        else:  # no unit: as written, or in millions / billions ("727" for $727M)
            cands = [(value, step), (value * 1e6, step * 1e6), (value * 1e9, step * 1e9)]
        if negative:
            cands = [(-v, t) for v, t in cands]
        out.append(Mention(m.group(0).strip(), tuple(cands)))
    return out


def _close(x: float, target: float, tol: float) -> bool:
    return abs(x - target) <= tol + REL_TOLERANCE * abs(target) + 1e-12


def _matches(mention: Mention, value: float) -> bool:
    return any(_close(v, value, t) or _close(abs(v), abs(value), t)
               for v, t in mention.candidates)  # fmt: skip


def _computations(points: list[DataPoint]) -> list[tuple[float, str]]:
    vals = [(p.value, p.id) for p in points if p.value is not None]
    out: list[tuple[float, str]] = []
    for (a, ia), (b, ib) in itertools.permutations(vals, 2):
        out.append((a + b, f"{ia} + {ib}"))
        out.append((a - b, f"{ia} - {ib}"))
        if b:
            out.append((a / b, f"{ia} / {ib}"))
            out.append(((a - b) / abs(b), f"change from {ib} to {ia}"))
    by_unit: dict[str, list[tuple[float, str]]] = {}
    for p in points:
        if p.value is not None:
            by_unit.setdefault(p.unit, []).append((p.value, p.id))
    for items in by_unit.values():
        if len(items) > 2:
            out.append((sum(v for v, _ in items), "sum of " + ", ".join(i for _, i in items)))
    return out


def _context_texts(ctx: ChatContext) -> list[str]:
    extra = ctx.model_extra or {}
    texts = [f.get("message", "") for f in extra.get("dq_findings") or [] if isinstance(f, dict)]
    texts.append(str(extra.get("coverage_note") or ""))
    texts += [str(v) for v in (extra.get("company_overrides") or {}).values()]
    for p in ctx.periods:
        texts.append(str(p.get("nm_reason") or ""))
        tags = [*p.get("tags", []), *(t for i in p.get("inputs", []) for t in i.get("tags", []))]
        texts += [str(t.get("proxy") or "") for t in tags]
    return [t for t in texts if t]


def _stated(ctx: ChatContext) -> list[Mention]:
    """Numbers the context's text states (D-091), e.g. a finding's "-707% vs a year earlier"."""
    return [m for text in _context_texts(ctx) for m in mentions(text)]


def check_answer(answer: ChatAnswer, ctx: ChatContext, question: str = "") -> Check:
    known = ctx.points()
    unknown = [c for c in answer.citations if c not in known]
    if unknown:
        return Check(False, f"cites unknown data points: {', '.join(unknown[:5])}")
    cited = [known[c] for c in dict.fromkeys(answer.citations)]
    peer = (ctx.model_extra or {}).get("peer_median")
    peers = peer.get("peers") if isinstance(peer, dict) else None
    found = mentions(answer.answer)
    if answer.issue_summary:
        found += mentions(answer.issue_summary)
    computed: list[str] = []
    added: list[str] = []
    echoed: list[str] = []
    unmatched: list[str] = []
    asked = mentions(question)
    combos: list[tuple[float, str]] | None = None
    stated: list[Mention] | None = None
    for mention in found:
        if peers is not None and any(_close(v, peers, 0) for v, _ in mention.candidates):
            continue
        if any(p.value is not None and _matches(mention, p.value) for p in cited):
            continue
        near = [p for p in ctx.data_points if p.value is not None and _matches(mention, p.value)]
        if near:  # the closest, when several are within rounding (2.91B: 2.909B, not 2.926B)
            other = min(near, key=lambda p: min(abs(abs(v) - abs(p.value or 0))
                                                for v, _ in mention.candidates))  # fmt: skip
            if other.id not in added:
                added.append(other.id)
            continue
        if combos is None:
            combos = _computations(cited)
        how = next((label for value, label in combos if _matches(mention, value)), None)
        if how is not None:
            computed.append(f"{mention.text} = {how}")
            continue
        if stated is None:
            stated = _stated(ctx)
        if any(_matches(mention, v) for s in stated for v, _ in s.candidates):
            continue
        if any(_matches(mention, v) for q in asked for v, _ in q.candidates):
            echoed.append(mention.text)
            continue
        unmatched.append(mention.text)
    if unmatched:
        return Check(False, "numbers not found in the context: " + ", ".join(unmatched[:5]),
                     computed, added, echoed, unmatched)  # fmt: skip
    return Check(True, "", computed, added, echoed)
