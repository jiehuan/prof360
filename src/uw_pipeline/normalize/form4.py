"""Form 4 (statement of changes in beneficial ownership) XML -> transactions (Phase 15b, D-050).

Only non-derivative transactions (shares held, not options). Codes that matter: S open-market
sale, P open-market purchase; others (M exercise, F tax withholding, A award, G gift) are kept
for audit but are not open-market trades. A transaction counts as under a Rule 10b5-1 plan when
a footnote it refers to says so; when no line refers to such a footnote, the filing's 10b5-1
box (2023 onward) applies to every line.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

_PLAN = re.compile(r"10b5-1", re.I)
_NOT_PLAN = re.compile(r"\bnot\b[^.]{0,60}10b5-1", re.I)
_AMOUNTS = "transactionAmounts"
_AFTER = "postTransactionAmounts/sharesOwnedFollowingTransaction/value"


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "y", "yes")


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_form4(xml: bytes) -> dict[str, Any]:
    """{issuer_cik, owner: {cik, name, is_director, is_officer, officer_title, is_ten_percent},
    transactions: [{line, security_title, trade_date, code, acquired_disposed, shares, price,
    shares_after, direct, is_10b5_1}]}. Joint filings: the first reporting owner. The issuer
    can be another company: a company's EDGAR list also has the Form 4s it files as an owner."""
    root = ET.fromstring(xml)
    owner_el = root.find("reportingOwner")
    rel = owner_el.find("reportingOwnerRelationship") if owner_el is not None else None
    owner = {
        "cik": _text(owner_el, "reportingOwnerId/rptOwnerCik"),
        "name": _text(owner_el, "reportingOwnerId/rptOwnerName"),
        "is_director": _flag(_text(rel, "isDirector")),
        "is_officer": _flag(_text(rel, "isOfficer")),
        "officer_title": _text(rel, "officerTitle"),
        "is_ten_percent": _flag(_text(rel, "isTenPercentOwner")),
    }
    plan_box = _flag(_text(root, "aff10b5One"))
    plan_notes = set()
    for f in root.iter("footnote"):
        text = "".join(f.itertext())
        if _PLAN.search(text) and not _NOT_PLAN.search(text):
            plan_notes.add(f.get("id"))
    lines = list(root.iter("nonDerivativeTransaction"))
    cited = [{f.get("id") for f in t.iter("footnoteId")} for t in lines]
    by_note = any(notes & plan_notes for notes in cited)
    out = []
    for i, (t, notes) in enumerate(zip(lines, cited, strict=True), start=1):
        out.append({
            "line": i,
            "security_title": _text(t, "securityTitle/value"),
            "trade_date": _text(t, "transactionDate/value"),
            "code": _text(t, "transactionCoding/transactionCode"),
            "acquired_disposed": _text(t, f"{_AMOUNTS}/transactionAcquiredDisposedCode/value"),
            "shares": _float(_text(t, f"{_AMOUNTS}/transactionShares/value")),
            "price": _float(_text(t, f"{_AMOUNTS}/transactionPricePerShare/value")),
            "shares_after": _float(_text(t, _AFTER)),
            "direct": _text(t, "ownershipNature/directOrIndirectOwnership/value") != "I",
            "is_10b5_1": bool(notes & plan_notes) if by_note else plan_box,
        })  # fmt: skip
    issuer = _text(root, "issuer/issuerCik")
    return {"issuer_cik": int(issuer) if issuer and issuer.isdigit() else None,
            "owner": owner, "transactions": out}  # fmt: skip
