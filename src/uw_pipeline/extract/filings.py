"""Periodic filing list (from SEC submissions) and XBRL instance lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from uw_pipeline.clients.sec import SecClient
from uw_pipeline.normalize.xbrl_facts import PERIODIC_FORMS

_LINKBASE = re.compile(r"_(cal|def|lab|pre)\.xml$", re.IGNORECASE)


@dataclass(frozen=True)
class Filing:
    accn: str
    form: str
    filed: str
    report_date: str
    primary_doc: str
    is_xbrl: bool


def _rows(block: dict[str, Any]) -> list[Filing]:
    n = len(block.get("accessionNumber", []))
    return [
        Filing(
            accn=block["accessionNumber"][i],
            form=block["form"][i],
            filed=block["filingDate"][i],
            report_date=block["reportDate"][i] or "",
            primary_doc=block["primaryDocument"][i],
            is_xbrl=bool(block["isXBRL"][i]),
        )
        for i in range(n)
    ]


def periodic_filings(
    sec: SecClient,
    cik: int | str,
    submissions: dict[str, Any],
    since: date,
    *,
    force: bool = False,
) -> list[Filing]:
    """10-K/10-Q (and amendments) filed on or after `since`, oldest first.

    `filings.recent` holds the latest ~1000 filings. For frequent filers (JPM files hundreds of
    424B2s a month) that is under a year, so older pages are fetched when the window needs them.
    """
    filings = submissions["filings"]
    rows = _rows(filings["recent"])
    for page in filings.get("files", []):
        if page["filingTo"] < since.isoformat():
            continue
        rows += _rows(sec.submissions_page(cik, page["name"], force=force).payload)
    keep = {
        f.accn: f
        for f in rows
        if f.form in PERIODIC_FORMS and f.is_xbrl and f.filed >= since.isoformat()
    }
    return sorted(keep.values(), key=lambda f: (f.filed, f.accn))


def instance_name(names: list[str], primary_doc: str) -> str | None:
    """The XBRL instance file in a filing folder.

    Inline XBRL filings publish an extracted instance named '<primary>_htm.xml'. Older filings
    have a standalone '<ticker>-<date>.xml'.
    """
    stem = primary_doc.rsplit(".", 1)[0]
    if f"{stem}_htm.xml" in names:
        return f"{stem}_htm.xml"
    htm = [n for n in names if n.endswith("_htm.xml")]
    if htm:
        return htm[0]
    plain = [
        n
        for n in names
        if n.endswith(".xml") and not _LINKBASE.search(n) and n != "FilingSummary.xml"
    ]
    return plain[0] if plain else None
