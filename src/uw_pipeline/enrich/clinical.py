"""Clinical-stage pipeline facts (Phase 17c, D-058): grounding, the two models' agreement, and
the text the insight package carries.

A program read by a model is kept only when its quote is found in the filing and names the
candidate (or the text just before it does, a heading) and its phase; a milestone only when
its own quote is found and contains the timing. Of one filing, only programs both models
found with the same phase are kept (paired by name and indication words); a milestone only
when both give the same timing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import Connection, select

from uw_pipeline.enrich.grounding import is_grounded, normalize
from uw_pipeline.enrich.schemas import ClinicalProgramRead
from uw_pipeline.schema import clinical_program, source_document

PAIR_AT = 0.5  # name + indication word overlap at which two readings are one program
# Words that show a phase in the quote (lower case, normalized text).
_PHASE_WORDS = {
    "preclinical": r"pre-?clinical|ind-enabling|discovery|research stage",
    "phase_1": r"phase\s*(?:1|i)\b(?![/-])|phase\s*1[ab]\b(?![/-])|first-in-human",
    "phase_1_2": r"phase\s*(?:1|i)[ab]?\s*/\s*(?:2|ii)|phase\s*1[ab]?\s*-\s*2",
    "phase_2": r"phase\s*(?:2|ii)[ab]?\b(?![/-])",
    "phase_2_3": r"phase\s*(?:2|ii)[ab]?\s*/\s*(?:3|iii)|phase\s*2[ab]?\s*-\s*3",
    "phase_3": r"phase\s*(?:3|iii)\b|pivotal|registrational",
    "submitted": r"\b(?:nda|bla|maa|pma|snda|sbla)\b|submitted|submission|under review|pdufa",
    "approved": r"approv",
}
_STOP = frozenset({"a", "and", "for", "in", "of", "or", "the", "to", "with", "patients",
                   "treatment", "disease", "adults"})  # fmt: skip
_PHASE_ORDER = list(_PHASE_WORDS)


@dataclass(frozen=True)
class Program:
    name: str
    indication: str
    phase: str
    quote: str
    next_milestone: str | None
    milestone_timing: str | None
    milestone_quote: str | None


def tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", normalize(text))
                     if len(w) > 1 and w not in _STOP)  # fmt: skip


def _name_in(name: str, quote: str) -> bool:
    """The candidate's name (or its first word, e.g. a code before a parenthesis) is in the
    quote."""
    q = normalize(quote)
    words = re.findall(r"[a-z0-9][a-z0-9\-]*", normalize(name))
    return bool(words) and (normalize(name) in q or words[0] in q)


def _name_near(name: str, quote: str, source: str, before: int = 1_500) -> bool:
    """The name is in the quote, or in the text just before it: filings describe a candidate
    under a heading with its name and do not repeat it ("Barzolvolimab ... In February 2026,
    we announced that enrollment is complete in our Phase 3 studies in CSU")."""
    if _name_in(name, quote):
        return True
    src, q = normalize(source), normalize(quote).strip("\"'")
    at = src.find(q[:60])
    return at >= 0 and _name_in(name, src[max(0, at - before) : at + len(q)])


def checked(reads: Iterable[ClinicalProgramRead], source: str) -> list[Program]:
    """Programs whose quote is in the filing and names the candidate and its phase; a milestone
    whose quote is missing, or lacks the timing, is dropped (the program is kept)."""
    out = []
    for r in reads:
        quote = r.quote.strip().strip("'\",;").strip()
        name = r.name.strip()
        if len(name) < 2 or not quote or not is_grounded(quote, source):
            continue
        if not _name_near(name, quote, source) or not re.search(_PHASE_WORDS[r.phase],
                                                                 normalize(quote)):  # fmt: skip
            continue
        milestone, timing = r.next_milestone, r.milestone_timing
        mq: str | None = (r.milestone_quote or "").strip().strip("'\",;").strip()
        if not (milestone and timing and mq and is_grounded(mq, source)
                and normalize(timing) in normalize(mq)):  # fmt: skip
            milestone = timing = mq = None
        out.append(Program(
            name=name[:200], indication=r.indication.strip()[:300], phase=r.phase,
            quote=quote, next_milestone=milestone.strip()[:300] if milestone else None,
            milestone_timing=timing.strip()[:100] if timing else None,
            milestone_quote=mq or None,
        ))  # fmt: skip
    return out


_PAREN = re.compile(r"\([^)]*\)")


def _similarity(a: Program, b: Program) -> float:
    """Name and indication words in common; the indication without its parenthetical detail
    ("inflammatory diseases (TSLP and SCF bispecific)" is "inflammatory diseases")."""
    na, nb = tokens(a.name), tokens(b.name)
    if not na or not nb or not (na & nb):
        return 0.0  # different candidates whatever the indication
    ta = na | tokens(_PAREN.sub(" ", a.indication) or a.indication)
    tb = nb | tokens(_PAREN.sub(" ", b.indication) or b.indication)
    return len(ta & tb) / len(ta | tb)


def agreed(first: Sequence[Program], second: Sequence[Program]) -> tuple[list[Program], int]:
    """Programs both models found with the same phase: (kept, readings left unpaired). The
    milestone is kept where both give the same timing."""
    pairs = sorted(((_similarity(a, b), i, j) for i, a in enumerate(first)
                    for j, b in enumerate(second)), reverse=True)  # fmt: skip
    used_a: set[int] = set()
    used_b: set[int] = set()
    kept = []
    for score, i, j in pairs:
        if score < PAIR_AT or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        a, b = first[i], second[j]
        if a.phase != b.phase:
            continue
        same = (a.milestone_timing and b.milestone_timing
                and normalize(a.milestone_timing) == normalize(b.milestone_timing))  # fmt: skip
        kept.append(a if same else replace(a, next_milestone=None, milestone_timing=None,
                                           milestone_quote=None))  # fmt: skip
    unpaired = len(first) - len(used_a) + len(second) - len(used_b)
    return kept, unpaired


def program_key(p: Program) -> str:
    """Name and indication words: one row per candidate and indication."""
    name = "-".join(sorted(tokens(p.name))) or "unnamed"
    return f"{name}:{'-'.join(sorted(tokens(p.indication)))}"[:200]


def same_program(p: Program, row: dict[str, Any]) -> bool:
    """A stored program (by name and indication words) that this reading updates."""
    other = Program(row["name"], row["indication"], row["phase"], "", None, None, None)
    return _similarity(p, other) >= PAIR_AT


def clinical_text(programs: Sequence[dict[str, Any]]) -> str:
    """The current programs, most advanced first, each with the filing's own words."""
    ranked = sorted(programs, key=lambda p: -_PHASE_ORDER.index(p["phase"]))
    lines = []
    for p in ranked:
        head = f"{p['name']} ({p['indication']}; {p['phase'].replace('_', ' ')})"
        if p["next_milestone"]:
            head += f"; next: {p['next_milestone']}, {p['milestone_timing']}"
            lines.append(f'{head}. "{p["quote"][:400]}" "{p["milestone_quote"][:400]}"')
        else:
            lines.append(f'{head}. "{p["quote"][:400]}"')
    return "\n".join(lines)


def current_programs(conn: Connection, company_id: int) -> list[dict[str, Any]]:
    """The company's current programs with the filed date of the filing each comes from."""
    rows = conn.execute(
        select(clinical_program, source_document.c.filed_at)
        .join(source_document, source_document.c.doc_id == clinical_program.c.doc_id)
        .where(clinical_program.c.company_id == company_id, clinical_program.c.is_current)
        .order_by(clinical_program.c.program_key)
    ).mappings()  # fmt: skip
    return [dict(r) for r in rows]
