"""Litigation and regulatory matters (Phase 15e, D-050): grounding, the two models' agreement,
and matching a filing's matters to the ones already stored.

A matter read by a model is kept only when its quote is found in the filing; a start date or an
accrual only when it is in that quote. Of one filing, only matters both models found with the
same type are kept (paired by name, or by overlapping quotes). Across filings a matter is the
same when its type is the same and its name shares enough words (`MERGE_AT`), or its name is
nearly the same whatever the type (an investigation that became an enforcement action).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select

from uw_pipeline.enrich.grounding import amount_in, is_grounded, normalize
from uw_pipeline.enrich.schemas import LegalMatterRead
from uw_pipeline.schema import legal_matter, review_decision, source_document

PAIR_AT = 0.5  # name overlap at which the two models' readings of one filing are one matter
MERGE_AT = 0.6  # name overlap at which a filing's matter is a stored one
_STOP = frozenset({
    "a", "al", "an", "and", "action", "actions", "by", "case", "co", "company", "corp",
    "corporation", "et", "for", "in", "inc", "litigation", "llc", "ltd", "matter", "matters",
    "of", "on", "plc", "re", "regarding", "related", "relating", "the", "to", "v", "vs", "with",
})  # fmt: skip
# Words that describe a kind of matter rather than which one: two names that differ only in
# these ("Asbestos personal injury lawsuits", "Asbestos product liability lawsuits") match.
_GENERIC = frozenset({
    "civil", "claim", "claims", "class", "complaint", "complaints", "consumer", "court",
    "courts", "demand", "demands", "derivative", "district", "federal", "government",
    "individual", "infringement", "injuries", "injury", "inquiries", "inquiry", "investigation",
    "investigations", "lawsuit", "lawsuits", "liability", "mdl", "multidistrict", "other",
    "patent", "patents", "personal", "proceeding", "proceedings", "product", "products",
    "putative", "securities", "shareholder", "shareholders", "state", "states", "stockholder",
    "stockholders", "subpoena", "subpoenas", "suit", "suits",
})  # fmt: skip
CROSS_TYPE_AT = 0.8  # name overlap (all words) at which a matter matches whatever its type
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PAREN = re.compile(r"\([^)]*\)")
_FULL_DATE = re.compile(
    r"^(?:january|february|march|april|may|june|july|august|september|october|november|"
    r"december) \d{1,2}, (?:19|20)\d{2}$"
)


@dataclass(frozen=True)
class Matter:
    name: str
    matter_type: str
    agency: str | None
    status: str
    started: str | None
    wells_notice: bool
    accrual: float | None  # USD
    quote: str


def tokens(name: str) -> frozenset[str]:
    return frozenset(
        w for w in re.findall(r"[a-z0-9]+", normalize(name)) if len(w) > 1 and w not in _STOP
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(a: str, b: str, ignore: frozenset[str] = frozenset()) -> float:
    """Share of name words in common (Jaccard), ignoring captions' filler words and `ignore`
    (the company's own name). 1.0 when the distinctive words of one name, without the
    parentheticals models add ("(S.D. Tex.)"), are all in the other ("Docetaxel product
    liability MDL" and "Docetaxel hair loss and eye injury litigation")."""
    score = _jaccard(tokens(a) - ignore, tokens(b) - ignore)
    ca = tokens(_PAREN.sub(" ", a)) - _GENERIC - ignore
    cb = tokens(_PAREN.sub(" ", b)) - _GENERIC - ignore
    if ca and cb and (ca <= cb or cb <= ca):
        return 1.0
    return max(score, _jaccard(ca, cb))


def same_subject(a: str, a_agency: str | None, b: str, b_agency: str | None,
                 ignore: frozenset[str] = frozenset()) -> bool:  # fmt: skip
    """A government matter: the same agency and a distinctive name word in common besides the
    agency's own ("Huawei ... FCC Letter of Inquiry", "FCC Enforcement Bureau ... Huawei")."""
    ta, tb = tokens(a_agency or ""), tokens(b_agency or "")
    if not ta or not tb or not (ta <= tb or tb <= ta):
        return False
    words = _GENERIC | ignore | ta | tb | {"letter", "enforcement", "bureau", "office"}
    return bool((tokens(_PAREN.sub(" ", a)) - words) & (tokens(_PAREN.sub(" ", b)) - words))


def same_start(a: str | None, b: str | None) -> bool:
    """Both give the same full date ("January 10, 2023"): one suit or investigation."""
    na, nb = normalize(a or ""), normalize(b or "")
    return bool(_FULL_DATE.match(na)) and na == nb


def checked(reads: Iterable[LegalMatterRead], source: str) -> list[Matter]:
    """Matters whose quote is in the filing; a start date or accrual not in the quote is
    dropped (the matter is kept)."""
    out = []
    for r in reads:
        quote = r.quote.strip().strip("'\",;").strip()  # JSON debris: appeal.','"
        if len(r.name.strip()) < 3 or not quote or not is_grounded(quote, source):
            continue
        year = _YEAR.search(r.started or "")
        started = r.started.strip()[:30] if r.started and year and year[0] in quote else None
        accrual = r.accrual_usd if r.accrual_usd and r.accrual_usd > 0 and amount_in(
            r.accrual_usd, quote) else None  # fmt: skip
        out.append(Matter(
            name=r.name.strip()[:300], matter_type=r.matter_type,
            agency=(r.agency or "").strip()[:100] or None, status=r.status, started=started,
            wells_notice=r.wells_notice and "wells" in normalize(quote), accrual=accrual,
            quote=quote,
        ))  # fmt: skip
    return out


def _quotes_overlap(a: str, b: str) -> bool:
    qa, qb = normalize(a), normalize(b)
    return qa[:60] in qb or qb[:60] in qa


def _pair_score(a: Matter, b: Matter, ignore: frozenset[str]) -> float:
    if a.matter_type != b.matter_type:
        return 0.0
    if _quotes_overlap(a.quote, b.quote) or same_start(a.started, b.started):
        return 1.0
    if same_subject(a.name, a.agency, b.name, b.agency, ignore):
        return 0.9
    return similarity(a.name, b.name, ignore)


def agreed(
    first: Sequence[Matter], second: Sequence[Matter], ignore: frozenset[str] = frozenset()
) -> tuple[list[Matter], int]:
    """Matters both models found with the same type: (kept, readings left unpaired). Status,
    start, accrual and agency are kept where both agree; otherwise unknown / None."""
    pairs = sorted(
        ((_pair_score(a, b, ignore), i, j)
         for i, a in enumerate(first) for j, b in enumerate(second)),
        reverse=True,
    )  # fmt: skip
    used_a: set[int] = set()
    used_b: set[int] = set()
    kept = []
    for score, i, j in pairs:
        if score < PAIR_AT or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        a, b = first[i], second[j]
        same_year = a.started and b.started and _YEAR.findall(a.started) == _YEAR.findall(
            b.started)  # fmt: skip
        same_accrual = a.accrual is not None and b.accrual is not None and abs(
            a.accrual - b.accrual) <= 0.005 * max(a.accrual, b.accrual)  # fmt: skip
        kept.append(Matter(
            name=a.name, matter_type=a.matter_type,
            agency=a.agency if a.agency and b.agency else None,
            status=a.status if a.status == b.status else "unknown",
            started=a.started if same_year else None,
            wells_notice=a.wells_notice and b.wells_notice,
            accrual=a.accrual if same_accrual else None, quote=a.quote,
        ))  # fmt: skip
    unpaired = len(first) - len(used_a) + len(second) - len(used_b)
    return kept, unpaired


def matter_key(m: Matter) -> str:
    return f"{m.matter_type}:{'-'.join(sorted(tokens(m.name))) or 'unnamed'}"[:160]


def match(
    m: Matter, stored: Iterable[Mapping[str, Any]], ignore: frozenset[str] = frozenset()
) -> Mapping[str, Any] | None:
    """The stored matter whose name is closest, if close enough: of the same type (the same
    full start date, or agency and subject, also count), or of another type with nearly the
    same name."""
    best, score = None, 0.0
    for s in stored:
        if s["matter_type"] == m.matter_type:
            same = s["matter_key"] == matter_key(m) or same_start(m.started, s["started"])
            sim = 1.0 if same else similarity(m.name, s["name"], ignore)
            if same_subject(m.name, m.agency, s["name"], s["agency"], ignore):
                sim = max(sim, 0.9)
        else:  # all words, generic ones included: "Zantac MDL" is not "Zantac state actions"
            full = _jaccard(tokens(m.name) - ignore, tokens(s["name"]) - ignore)
            sim = full - 0.01 if full >= CROSS_TYPE_AT else 0.0  # same type wins a tie
        if sim > score:
            best, score = s, sim
    return best if score >= MERGE_AT else None


def review_key(company_id: int, key: str) -> str:
    """The matter's id in review_decision (`uw-pipeline review`)."""
    return f"{company_id}:{hashlib.sha1(key.encode()).hexdigest()[:24]}"


def legal_matters(conn: Connection, company_id: int) -> list[dict[str, Any]]:
    """The company's litigation and regulatory matters (Phase 15e), without those a reviewer
    rejected, with the accession of the last filing that mentions each."""
    rejected = {k for (k,) in conn.execute(select(review_decision.c.item_key).where(
        review_decision.c.item_type == "MATTER", review_decision.c.company_id == company_id,
        review_decision.c.decision == "REJECTED"))}  # fmt: skip
    rows = conn.execute(
        select(legal_matter, source_document.c.accession_no)
        .join(source_document, source_document.c.doc_id == legal_matter.c.last_doc_id)
        .where(legal_matter.c.company_id == company_id)
        .order_by(legal_matter.c.matter_key)
    ).mappings()  # fmt: skip
    return [dict(r) for r in rows if review_key(company_id, r["matter_key"]) not in rejected]
