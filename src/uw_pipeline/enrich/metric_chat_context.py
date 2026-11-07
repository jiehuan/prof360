"""Metric chat context (Phase 19, D-088, D-090): what the chat in the metric popup may use about
one company's metric, as JSON. Rules only, no LLM.

Every number the chat may cite is a data point with a stable id, `dp:<period>:<name>`
(`dp:FY2025:interest_expense`, `dp:2026Q3:ebit`; the peer median is
`dp:FY2025:interest_coverage:peer_median`). The rest of the context says where each number
comes from: the formula, each period's inputs with their XBRL tags (dimensional, a fallback
candidate, a company override and its reason), the filing, why a value is N/M, the data-quality
findings and coverage notes for the metric, and the industry group's median.

Built per company by the metricdetail stage (metric_chat_context) and published to
MetricChatContext; the BFF reads it with udf_GetMetricChatContext.
"""

from __future__ import annotations

import ast
import hashlib
import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, and_, func, select

from uw_pipeline.compute.registry import CompanyRegistry, MetricDef
from uw_pipeline.enrich.metric_detail import (
    LLM_DERIVATIONS,
    definition_lines,
    period_label,
    readable_expr,
    shown_rows,
)
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.enrich.sources import SourceLabels, SourceRef
from uw_pipeline.schema import company, dq_finding, metric_input, metric_value

# Bump when the JSON's shape changes; the BFF's chat package states the versions it reads.
CONTEXT_VERSION = 1
MIN_PEERS = 3  # no peer median from fewer companies

# Values whose source is the filing's XBRL (cited by accession number); others name a document.
XBRL_DERIVATIONS = ("REPORTED", "DERIVED", "COMPUTED")
# Units of concepts (concept_map.yaml `unit`) as metric units.
_CONCEPT_UNITS = {"pure": "fraction", "shares": "shares"}


def period_id(key: tuple[str, int, int]) -> str:
    """FY2025 | 2026Q3: the period part of a data-point id."""
    ptype, fy, fq = key
    return f"FY{fy}" if ptype == "FY" else f"{fy}Q{fq}"


def _row_key(row: Mapping[str, Any]) -> tuple[str, int, int]:
    return (row["period_type"], int(row["fiscal_year"]), int(row["fiscal_quarter"]))


def dp_id(period: str, name: str) -> str:
    return f"dp:{period}:{name}"


def id_label(period: str) -> str:
    """2026Q3 -> FY2026 Q3, as the popup labels periods."""
    return period if period.startswith("FY") else f"FY{period[:4]} Q{period[-1]}"


@dataclass
class CompanyData:
    """One company's inputs to its metrics' contexts, read once per company."""

    company: Mapping[str, Any]  # pipeline company row
    group_code: str
    group_label: str
    creg: CompanyRegistry
    labels: SourceLabels
    names: Mapping[str, str]  # metric and concept code -> display name
    values: Mapping[str, list[dict[str, Any]]]  # metric code -> all its metric_value rows
    inputs: Mapping[tuple[str, str, int, int], list[dict[str, Any]]]  # metric_input, parsed
    findings: Mapping[str, list[dict[str, Any]]]  # metric code -> latest dq findings
    coverage: Mapping[str, str]  # metric code (or "*") -> coverage note
    peers: Mapping[str, dict[str, Any]]  # metric code -> peer median
    source_of: Callable[[MetricDef, dict[str, Any]], SourceRef]  # a stored value's document
    filing: Callable[[str], SourceRef]  # an accession number's filing
    _units: dict[str, str] = field(default_factory=dict)

    def unit_of(self, name: str) -> str:
        """The unit of a formula name: a metric's own, else its concept's."""
        if not self._units:
            self._units = {m.code: self.creg.unit_of(m) for m in self.creg.metrics}
        base = name.removesuffix("_ttm")
        if base in self._units:
            return self._units[base]
        unit = self.creg.units.get(base, "USD")
        return _CONCEPT_UNITS.get(unit, self.creg.currency if unit == "USD" else unit)


# -- reading -----------------------------------------------------------------------------------


def read_inputs(conn: Connection, company_id: int) -> dict[tuple[str, str, int, int], list[Any]]:
    c = metric_input.c
    return {
        (r.metric_code, r.period_type, r.fiscal_year, r.fiscal_quarter): json.loads(r.inputs_json)
        for r in conn.execute(select(metric_input).where(c.company_id == company_id))
    }


def read_findings(conn: Connection, company_id: int) -> dict[str, list[dict[str, Any]]]:
    """The metric-level findings of the latest run that checked this company. The dq stage runs
    after metricdetail, so these are the previous run's until the next one."""
    f = dq_finding.c
    latest = conn.execute(
        select(func.max(f.run_id)).where(f.company_id == company_id)
    ).scalar_one_or_none()
    if latest is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        select(f.rule_id, f.severity, f.metric_code, f.period, f.message)
        .where(f.company_id == company_id, f.run_id == latest, f.metric_code.is_not(None))
        .order_by(f.finding_id)
    ):  # fmt: skip
        out.setdefault(r.metric_code, []).append(
            {"rule": r.rule_id, "severity": r.severity, "period": r.period,
             "message": r.message})  # fmt: skip
    return out


def peer_medians(
    conn: Connection,
    company_id: int,
    group_code: str,
    own: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Per metric, the median of the other active companies in the industry group for the
    company's own latest fiscal year (same unit), when at least MIN_PEERS have a value."""
    latest: dict[str, tuple[int, str]] = {}
    for code, rows in own.items():
        fy = [r for r in rows if r["period_type"] == "FY" and r["value_status"] == "OK"]
        if fy:
            top = max(fy, key=lambda r: r["fiscal_year"])
            latest[code] = (int(top["fiscal_year"]), top["unit"])
    if not latest:
        return {}
    v, co = metric_value.c, company.c
    found: dict[tuple[str, int, str], list[float]] = {}
    for r in conn.execute(
        select(v.metric_code, v.fiscal_year, v.unit, v.value)
        .join(company, co.company_id == v.company_id)
        .where(and_(co.industry_group == group_code, co.is_active.is_(True),
                    co.company_id != company_id, v.period_type == "FY",
                    v.value_status == "OK", v.value.is_not(None),
                    v.metric_code.in_(sorted(latest))))
    ):  # fmt: skip
        found.setdefault((r.metric_code, r.fiscal_year, r.unit), []).append(float(r.value))
    out: dict[str, dict[str, Any]] = {}
    for code, (year, unit) in latest.items():
        vals = found.get((code, year, unit), [])
        if len(vals) >= MIN_PEERS:
            out[code] = {"fiscal_year": year, "median": statistics.median(vals),
                         "peers": len(vals)}  # fmt: skip
    return out


# -- building ----------------------------------------------------------------------------------


def _source(ref: SourceRef | None) -> dict[str, Any] | None:
    if ref is None or (ref.url is None and ref.date is None):
        return None
    return {"title": ref.title, "url": ref.url, "form": ref.form, "filed": ref.date}


def _tag_info(concept: str, label: str, d: CompanyData) -> dict[str, Any]:
    refs = d.creg.concepts.get(concept, [])
    candidates = [t.label() for t in refs]
    info: dict[str, Any] = {"concept": concept, "tag": label, "dimensional": "[" in label}
    if label in candidates:
        rank = candidates.index(label) + 1
        info["candidate"] = f"{rank} of {len(candidates)}"
        info["fallback"] = rank > 1
        if rank > 1:  # the tags the company did not report, tried first
            info["preferred_tags"] = candidates[: rank - 1][:4]
        if refs[rank - 1].proxy:  # close to the concept but not it (concept_map.yaml)
            info["proxy"] = refs[rank - 1].proxy
    if concept in d.creg.overridden:
        info["company_override"] = True  # its reason: company_overrides at the top
    return info


def _tags(tag_map: Mapping[str, Sequence[str]], d: CompanyData) -> list[dict[str, Any]]:
    return [_tag_info(concept, label, d) for concept, labels in tag_map.items()
            for label in labels]  # fmt: skip


def _num(value: float | None) -> float | None:
    """Values as stored, without float noise (22.209876543209877 -> 22.2098765432)."""
    return None if value is None else float(f"{value:.12g}")


class _Filings:
    """The filings a context cites, by accession number, listed once."""

    def __init__(self, filing: Callable[[str], SourceRef]) -> None:
        self.filing = filing
        self.items: dict[str, dict[str, Any]] = {}

    def add(self, accn: str | None) -> str | None:
        if not accn:
            return None
        if accn not in self.items:
            src = _source(self.filing(accn))
            if src is None:
                return None
            self.items[accn] = {"accession": accn, **src}
        return accn


class _Points:
    """The data points of one context, by id (first registration wins)."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(self, pid: str, label: str, value: float | None, unit: str, status: str) -> str:
        if pid not in self.items:
            self.items[pid] = {"id": pid, "label": label, "value": _num(value), "unit": unit,
                               "status": status}  # fmt: skip
        return pid


def _nm_reason(
    m: MetricDef, inputs: Sequence[Mapping[str, Any]], period: str, d: CompanyData
) -> str:
    bad = [i["name"] for i in inputs if i.get("status") == "NM"]
    if bad:
        names = ", ".join(d.names.get(n, n) for n in bad)
        return f"An input is itself not meaningful for {period}: {names}."
    if m.nm_when and inputs:
        known = {i["name"]: i["value"] for i in inputs if i["period"] == period}
        used = [n.id for n in ast.walk(ast.parse(m.nm_when, mode="eval"))
                if isinstance(n, ast.Name)]  # fmt: skip
        shown = [f"{d.names.get(n, n)} = {fmt_value(known[n], d.unit_of(n))}" for n in used
                 if known.get(n) is not None]  # fmt: skip
        detail = f" ({'; '.join(shown)})" if shown else ""
        return f"Not meaningful when {readable_expr(m.nm_when, d.names)}{detail}."
    if m.nm_when:
        return f"Not meaningful when {readable_expr(m.nm_when, d.names)}."
    return ("Not meaningful for this period: the quarter cannot be derived from the "
            "year-to-date and annual totals on one basis.")  # fmt: skip


def build_context(m: MetricDef, d: CompanyData) -> dict[str, Any]:
    """The chat context of metric `m` for the periods the popup shows."""
    unit = d.creg.unit_of(m)
    points, filings = _Points(), _Filings(d.filing)
    periods: list[dict[str, Any]] = []
    used: set[str] = set()  # derivations, explained once at the top
    for row in shown_rows(d.values.get(m.code, [])):
        key = _row_key(row)
        pid = period_id(key)
        row_unit = row["unit"] or unit
        value_id = points.add(dp_id(pid, m.code), f"{m.display_name} {period_label(row)}",
                              row["value"], row_unit, row["value_status"])  # fmt: skip
        p: dict[str, Any] = {
            "period": pid, "label": period_label(row), "period_end": row["period_end"],
            "value_id": value_id, "status": row["value_status"],
            "derivation": row["derivation"],
            "formula_used": readable_expr(row["formula"], d.names)
            if m.kind == "formula" and row["derivation"] == "COMPUTED" else row["formula"],
        }  # fmt: skip
        used.add(row["derivation"])
        if row["derivation"] in XBRL_DERIVATIONS:
            p["accession"] = filings.add(row["source_accn"])
        else:  # a document (MD&A, earnings release) or another source (Alpha Vantage, Form 4)
            p["source"] = _source(d.source_of(m, dict(row)))
        if row["derivation"] in LLM_DERIVATIONS and row.get("evidence"):
            p["evidence"] = row["evidence"]
        try:
            tag_map = json.loads(row["source_tags"] or "{}")
        except ValueError:
            tag_map = {}
        inputs = d.inputs.get((m.code, *key), [])
        if inputs:
            p["inputs"] = []
            for i in inputs:
                name, ipid = i["name"], i["period"]
                label = d.names.get(name, name.replace("_", " "))
                iu = d.unit_of(name)
                entry: dict[str, Any] = {
                    "id": points.add(dp_id(ipid, name), f"{label} {id_label(ipid)}", i["value"],
                                     iu, i["status"]),
                    "name": name, "label": label, "period": ipid,
                    "derivation": i["derivation"], "tags": _tags(i.get("tags", {}), d),
                }  # fmt: skip
                used.add(i["derivation"])
                accn = filings.add(i.get("accn")) if i["derivation"] in XBRL_DERIVATIONS else None
                if accn:
                    entry["accession"] = accn
                p["inputs"].append(entry)
        elif row["derivation"] in ("REPORTED", "DERIVED") and tag_map:
            p["tags"] = _tags(tag_map, d)
        if row["value_status"] == "NM":
            p["nm_reason"] = _nm_reason(m, inputs, pid, d)
        periods.append(p)

    ctx: dict[str, Any] = {
        "version": CONTEXT_VERSION,
        "company": {
            "ticker": d.company["ticker"], "name": d.company["name"],
            "industry_group": d.group_code, "industry_label": d.group_label,
            "fiscal_year_end": d.company.get("fiscal_year_end"), "currency": d.creg.currency,
        },
        "metric": {
            "code": m.code, "name": m.display_name, "subtitle": m.subtitle,
            "description": m.description, "unit": unit, "kind": m.kind,
            "definition": definition_lines(m, d.names),
            "nm_when": readable_expr(m.nm_when, d.names) if m.nm_when else None,
        },
        "derivations": {k: d.labels.derivations.get(k, k) for k in sorted(used)},
        "periods": periods,
    }  # fmt: skip
    peer = d.peers.get(m.code)
    if peer:
        fy = f"FY{peer['fiscal_year']}"
        ctx["peer_median"] = {
            "id": points.add(f"{dp_id(fy, m.code)}:peer_median",
                             f"{d.group_label} median {m.display_name} {fy}", peer["median"],
                             unit, "OK"),
            "fiscal_year": peer["fiscal_year"], "peers": peer["peers"],
            "group": d.group_label,
        }  # fmt: skip
    notes = {c: n for c, n in d.creg.notes.items() if c in _concepts(ctx)}
    if notes:
        ctx["company_overrides"] = notes
    if d.findings.get(m.code):
        ctx["dq_findings"] = d.findings[m.code]
    note = d.coverage.get(m.code) or d.coverage.get("*")
    if note:
        ctx["coverage_note"] = note
    ctx["filings"] = list(filings.items.values())
    ctx["data_points"] = list(points.items.values())
    return ctx


def _concepts(ctx: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for p in ctx["periods"]:
        for t in p.get("tags", []):
            found.add(t["concept"])
        for i in p.get("inputs", []):
            found |= {t["concept"] for t in i["tags"]}
    return found


def context_json(ctx: Mapping[str, Any]) -> tuple[str, str]:
    """(JSON text, sha256 of it): the hash decides whether the stored context changed."""
    text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return text, hashlib.sha256(text.encode()).hexdigest()
