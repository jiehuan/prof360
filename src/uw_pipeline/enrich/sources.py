"""Readable source titles and links for the front-end detail modals (Phase 8, decisions D-036).

Every insight, follow-up question and metric shows where its facts come from. Titles follow one
format, "<company> <form> filed <date> · <section>", and link to the SEC document (or the news
article). Earnings call transcripts have no public link (the Alpha Vantage URL needs a key), so
they get a title only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Connection, func, select

from uw_pipeline.clients.sec import archive_folder_url
from uw_pipeline.schema import activity, signal, source_document, xbrl_fact


class SourceLabels(BaseModel):
    sections: dict[str, str]
    roles: dict[str, str]
    derivations: dict[str, str]
    concepts: dict[str, str] = {}


def load_source_labels(config_dir: Path) -> SourceLabels:
    with (config_dir / "source_labels.yaml").open(encoding="utf-8") as f:
        return SourceLabels.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class SourceRef:
    title: str
    url: str | None
    date: str | None  # YYYY-MM-DD: filing date, event date or publication date
    doc_id: int | None = None
    form: str | None = None  # SEC form type, when the source is a filing


def filing_index_url(cik: str, accession_no: str) -> str:
    return f"{archive_folder_url(cik, accession_no)}/{accession_no}-index.html"


class SourceResolver:
    """Titles and links for one company's sources; lookups are cached per instance. The
    company's documents and the filings of its XBRL facts are read once, on first use (D-084:
    a statement per lookup was a round trip each on Azure SQL)."""

    def __init__(self, conn: Connection, company: dict[str, Any], labels: SourceLabels) -> None:
        self.conn, self.labels = conn, labels
        self.company_id: int = company["company_id"]
        self.cik: str = company["cik"]
        self.name: str = company["legal_name"]
        self._docs: dict[int, dict[str, Any] | None] = {}
        self._filings: dict[str, SourceRef] = {}
        self._roles: dict[tuple[str, str], int] | None = None  # (accession, role) -> doc id
        self._facts: dict[str, tuple[str, str]] = {}  # accession -> (form, filed date)

    def _load(self) -> dict[tuple[str, str], int]:
        if self._roles is None:
            self._roles = {}
            for row in self.conn.execute(
                select(source_document).where(source_document.c.company_id == self.company_id)
                .order_by(source_document.c.doc_id)
            ).mappings():  # fmt: skip
                self._docs.setdefault(row["doc_id"], dict(row))
                self._roles.setdefault((row["accession_no"], row["doc_role"]), row["doc_id"])
            xf = xbrl_fact.c
            self._facts = {a: (f, d) for a, f, d in self.conn.execute(
                select(xf.accession_no, func.min(xf.form), func.min(xf.filed_at))
                .where(xf.company_id == self.company_id).group_by(xf.accession_no))}  # fmt: skip
        return self._roles

    def doc_id(self, accession_no: str, role: str) -> int | None:
        """The company's document of this filing with this role (lowest id if several)."""
        return self._load().get((accession_no, role))

    def section_label(self, code: str | None) -> str | None:
        return None if code is None else self.labels.sections.get(code, code)

    def _doc(self, doc_id: int) -> dict[str, Any] | None:
        if doc_id not in self._docs:
            row = (
                self.conn.execute(select(source_document).where(source_document.c.doc_id == doc_id))
                .mappings()
                .first()
            )
            self._docs[doc_id] = dict(row) if row else None
        return self._docs[doc_id]

    def document(self, doc_id: int, section_code: str | None = None) -> SourceRef | None:
        d = self._doc(doc_id)
        if d is None:
            return None
        if d["source_type"] == "TRANSCRIPT":
            quarter = d["accession_no"].rsplit("-", 1)[-1]
            return SourceRef(
                f"{self.name} earnings call {quarter} (quarter ended {d['period_end']})",
                None, None, doc_id,
            )  # fmt: skip
        parts = [f"{self.name} {d['form_type']} filed {d['filed_at']}"]
        if d["doc_role"] != "PRIMARY":
            parts.append(self.labels.roles.get(d["doc_role"], d["doc_role"]))
        if section_code and section_code != "TRANSCRIPT":
            parts.append(self.section_label(section_code) or section_code)
        return SourceRef(" · ".join(parts), d["url"], d["filed_at"], doc_id, d["form_type"])

    def filing(self, accession_no: str) -> SourceRef:
        """The filing an XBRL value came from (its primary document when registered)."""
        if accession_no in self._filings:
            return self._filings[accession_no]
        d = self.doc_id(accession_no, "PRIMARY")
        ref: SourceRef | None = self.document(d) if d is not None else None
        if ref is None:
            fact = self._facts.get(accession_no)
            if fact is not None:
                form, filed = fact
                title = f"{self.name} {form} filed {filed}"
                ref = SourceRef(title, filing_index_url(self.cik, accession_no), filed,
                                form=form)  # fmt: skip
            else:
                ref = SourceRef(f"{self.name} SEC filing {accession_no}",
                                filing_index_url(self.cik, accession_no), None)  # fmt: skip
        self._filings[accession_no] = ref
        return ref

    def metric(self, label: str, accession_no: str | None) -> SourceRef:
        """`label` is the metric and period, e.g. "Total Revenue FY2026 Q2"."""
        if not accession_no:
            return SourceRef(label, None, None)
        f = self.filing(accession_no)
        return SourceRef(f"{label} — {f.title}", f.url, f.date, f.doc_id, f.form)

    def signal(self, signal_id: int) -> SourceRef | None:
        s = self.conn.execute(
            select(signal.c.title, signal.c.event_date, signal.c.doc_id, signal.c.section_code)
            .where(signal.c.signal_id == signal_id)
        ).first()  # fmt: skip
        if s is None:
            return None
        if s.doc_id is not None:
            ref = self.document(s.doc_id, s.section_code)
            if ref is not None:
                return SourceRef(ref.title, ref.url, s.event_date or ref.date, ref.doc_id)
        return SourceRef(f"{self.name} — {s.title}", None, s.event_date)

    def news(self, activity_id: int) -> SourceRef | None:
        a = self.conn.execute(
            select(activity.c.title, activity.c.source_name, activity.c.url,
                   activity.c.published_at)
            .where(activity.c.activity_id == activity_id)
        ).first()  # fmt: skip
        if a is None:
            return None
        return SourceRef(f"{a.source_name}: {a.title}", a.url, f"{a.published_at:%Y-%m-%d}")
