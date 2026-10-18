"""Filing lists (from SEC submissions) and lookups of files inside a filing."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import lxml.html

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
    items: str = ""  # 8-K item numbers, e.g. "2.02,9.01"


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
            items=(block.get("items") or [""] * n)[i] or "",
        )
        for i in range(n)
    ]


def filings_since(
    sec: SecClient,
    cik: int | str,
    submissions: dict[str, Any],
    since: date,
    forms: Iterable[str],
    *,
    force: bool = False,
) -> list[Filing]:
    """Filings of the given forms filed on or after `since`, oldest first.

    `filings.recent` holds the latest ~1000 filings. For frequent filers (JPM files hundreds of
    424B2s a month) that is under a year, so older pages are fetched when the window needs them.
    """
    wanted = set(forms)
    filings = submissions["filings"]
    rows = _rows(filings["recent"])
    for page in filings.get("files", []):
        if page["filingTo"] < since.isoformat():
            continue
        rows += _rows(sec.submissions_page(cik, page["name"], force=force).payload)
    keep = {f.accn: f for f in rows if f.form in wanted and f.filed >= since.isoformat()}
    return sorted(keep.values(), key=lambda f: (f.filed, f.accn))


def periodic_filings(
    sec: SecClient,
    cik: int | str,
    submissions: dict[str, Any],
    since: date,
    *,
    force: bool = False,
) -> list[Filing]:
    """10-K/10-Q (and amendments) with XBRL, filed on or after `since`, oldest first."""
    found = filings_since(sec, cik, submissions, since, PERIODIC_FORMS, force=force)
    return [f for f in found if f.is_xbrl]


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


def exhibit_by_type(index_html: bytes, doc_type: str) -> str | None:
    """File name of the document with the given type (e.g. "EX-99.1") in a filing's
    `<accession>-index.html` page. File names alone are unreliable ("er-6302026xerdoc.htm")."""
    doc = lxml.html.document_fromstring(index_html)
    rows = cast(
        list[lxml.html.HtmlElement], doc.xpath("//table[contains(@class, 'tableFile')]//tr")
    )
    for row in rows:
        cells = cast(list[lxml.html.HtmlElement], row.xpath("./td"))
        if len(cells) < 4:
            continue
        if cells[3].text_content().strip().upper() == doc_type.upper():
            links = cast(list[str], cells[2].xpath(".//a/@href"))
            if links:
                return str(links[0]).rsplit("/", 1)[-1].split("?")[0]
    return None


def ownership_parties(header: str) -> tuple[int | None, str | None]:
    """(subject company CIK, filer name) from a filing's index headers. A company's EDGAR
    listing holds Schedule 13Ds about it and the ones it files about others (Phase 15d)."""
    i, j = header.find("SUBJECT COMPANY"), header.find("FILED BY")
    if i < 0 or j < 0:
        return None, None
    subject = header[i:j] if i < j else header[i:]
    filer = header[j:i] if j < i else header[j:]
    cik = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", subject)
    name = re.search(r"COMPANY CONFORMED NAME:\s*([^\n<]+)", filer)
    return (int(cik.group(1)) if cik else None,
            html.unescape(name.group(1)).strip() if name else None)  # fmt: skip
