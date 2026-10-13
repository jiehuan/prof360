"""Metric detail modal, rule part (Phase 8, decisions D-036): the Formula / Evidence Methodology
text for one metric of one company, built from the metric definition and the stored values.
No LLM; the Underwriting Relevance is written by jobs/run_metric_detail.py.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from uw_pipeline.compute.registry import MetricDef
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.enrich.sources import SourceLabels

# The window shown on the front end: QoQ/YoY (8 quarters) and 5Ys.
QUARTERS_SHOWN = 8
YEARS_SHOWN = 5
LLM_DERIVATIONS = ("LLM_EXTRACTED", "PRELIMINARY")


def period_label(row: Mapping[str, Any]) -> str:
    if row["period_type"] == "FY":
        return f"FY{row['fiscal_year']}"
    return f"FY{row['fiscal_year']} Q{row['fiscal_quarter']}"


def shown_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows of one metric inside the front-end window, newest first."""
    q = sorted((r for r in rows if r["period_type"] == "Q"),
               key=lambda r: r["period_end"], reverse=True)[:QUARTERS_SHOWN]  # fmt: skip
    fy = sorted((r for r in rows if r["period_type"] == "FY"),
                key=lambda r: r["period_end"], reverse=True)[:YEARS_SHOWN]  # fmt: skip
    return [*q, *fy]


def latest(rows: Sequence[Mapping[str, Any]], ptype: str) -> Mapping[str, Any] | None:
    of_type = [r for r in rows if r["period_type"] == ptype]
    return max(of_type, key=lambda r: r["period_end"]) if of_type else None


def _readable(expr: str, names: Mapping[str, str]) -> str:
    """'total_debt / equity' -> 'Total Debt / Stockholders' Equity'."""
    tree = ast.parse(expr, mode="eval")
    out = expr
    for name in sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}, key=len,
                       reverse=True):  # fmt: skip
        label = names.get(name) or name.replace("_", " ").title()
        out = re.sub(rf"\b{re.escape(name)}\b", label, out)
    return out.replace("<=", "≤").replace(">=", "≥")


def _tags(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct XBRL tags behind the shown values, most used first."""
    count: dict[str, int] = {}
    for r in rows:
        try:
            tags = json.loads(r["source_tags"] or "{}")
        except ValueError:
            continue
        for v in tags.values():
            for t in v if isinstance(v, list) else []:
                count[t] = count.get(t, 0) + 1
    return sorted(count, key=lambda t: -count[t])


def methodology(
    m: MetricDef,
    rows: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    labels: SourceLabels,
) -> str:
    """Formula / Evidence Methodology for one metric; `rows` are all its stored values."""
    shown = shown_rows(rows)
    lines: list[str] = [m.description] if m.description else []
    if m.kind == "growth":
        base = names.get(m.base or "", m.base or "")
        than = "the prior quarter"
        if m.compare == "prior_year":
            than = "the same period a year earlier"
        lines.append(f"Formula: ({base} - {base} of {than}) / |{base} of {than}|.")
    elif m.kind == "formula":
        exprs = [_readable(f, names) for f in m.formula]
        if (
            len(m.formula) == 1
            and m.formula[0].isidentifier()
            and (m.formula[0] == m.code or m.formula[0] not in names)
        ):
            lines.append("Taken directly from the company's XBRL financial statements.")
        elif len(exprs) == 1:
            lines.append(f"Formula: {exprs[0]}.")
        else:
            lines.append("Formula: " + "; if not available, ".join(exprs) + ".")
        if m.optional:
            opt = ", ".join(names.get(o, o.replace("_", " ")) for o in m.optional)
            lines.append(f"Treated as zero when not reported: {opt}.")
    elif m.extraction is not None:
        lines.append(f"Definition: {m.extraction.definition}")
        check = (" Two models (Claude Sonnet 5.5 and Claude Opus 5.5) read the text "
                 "independently and the value is kept only where both agree.")  # fmt: skip
        lines.append(
            "Extracted from the MD&A of the 10-Q/10-K and from earnings releases; the number "
            "must appear verbatim in the quoted text." + (check if m.extraction.dual_check else "")
        )
    if m.nm_when:
        lines.append(f"Shown as N/M when {_readable(m.nm_when, names)}.")
    tags = _tags(shown)
    if tags and m.kind != "text":
        lines.append("XBRL tags used: " + ", ".join(tags[:6]) + ".")
    mix: dict[str, int] = {}
    for r in shown:
        mix[r["derivation"]] = mix.get(r["derivation"], 0) + 1
    if mix:
        nq = sum(1 for r in shown if r["period_type"] == "Q")
        nfy = len(shown) - nq
        parts = [f"{labels.derivations.get(d, d)}: {n}" for d, n in sorted(mix.items())]
        lines.append(f"Periods shown ({nq} quarters, {nfy} fiscal years) — " + "; ".join(parts)
                     + ".")  # fmt: skip
    if "DERIVED" in mix:
        lines.append("Quarterly values not reported on their own (typically Q4, and quarterly "
                     "cash flows) are derived from year-to-date and annual totals.")  # fmt: skip
    if "PRELIMINARY" in mix:
        lines.append("Preliminary values come from the earnings release and are replaced once "
                     "the 10-Q or 10-K is filed.")  # fmt: skip
    top = latest(rows, "Q") or latest(rows, "FY")
    if top is not None:
        lines.append(f"Latest: {fmt_value(top['value'], m.unit, top['value_status'])} "
                     f"({period_label(top)}, period ended {top['period_end']}).")  # fmt: skip
        if top["derivation"] in LLM_DERIVATIONS and top["evidence"]:
            lines.append(f"Source text: “{top['evidence'][:300]}”")
    return "\n".join(lines)
