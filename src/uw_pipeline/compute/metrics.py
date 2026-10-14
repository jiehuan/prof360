"""Standardized period values -> metric values. Pure rules; no LLM and no I/O."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from uw_pipeline.compute.registry import CompanyRegistry, MetricDef
from uw_pipeline.frames import records

PeriodKey = tuple[str, int, int]  # (period_type, fiscal_year, fiscal_quarter; 0 for FY)

MONETARY_UNIT = "USD"


@dataclass(frozen=True)
class Value:
    value: float | None  # None only when status == "NM"
    status: str  # OK | NM
    derivation: str  # REPORTED | DERIVED | COMPUTED
    period_end: str
    accn: str
    filed: str
    formula: str
    sources: tuple[tuple[str, str], ...] = field(default=())  # (concept, tag label)

    def source_map(self) -> dict[str, list[str]]:
        """Concept -> tags used, for lineage (a derived value can mix two candidate tags)."""
        out: dict[str, list[str]] = {}
        for concept, tag in self.sources:
            out.setdefault(concept, []).append(tag)
        return out


def concept_values(
    periods: pd.DataFrame, registry: CompanyRegistry
) -> dict[str, dict[PeriodKey, Value]]:
    """Per concept and period, the value of the first candidate tag that has one."""
    index: dict[tuple[str, str, str], dict[PeriodKey, dict[str, Any]]] = {}
    for row in records(periods):
        tag = f"{row['taxonomy']}:{row['tag']}"
        key: PeriodKey = (row["period_type"], int(row["fiscal_year"]), int(row["fiscal_quarter"]))
        index.setdefault((tag, row["dims"], row["unit"]), {})[key] = row

    out: dict[str, dict[PeriodKey, Value]] = {}
    for concept, candidates in registry.concepts.items():
        unit = registry.units.get(concept, MONETARY_UNIT)
        chosen: dict[PeriodKey, Value] = {}
        for ref in candidates:
            for key, row in index.get((ref.tag, ref.dims_key, unit), {}).items():
                if key in chosen:
                    continue
                chosen[key] = Value(
                    value=float(row["value"]),
                    status="OK",
                    derivation=row["derivation"],
                    period_end=row["period_end"],
                    accn=row["accn"],
                    filed=row["filed"],
                    formula=ref.label(),
                    sources=((concept, ref.label()),),
                )
        _fill_quarters_from_ytd(concept, chosen)
        out[concept] = {k: v for k, v in chosen.items() if k[0] != "YTD"}
    return out


def _ytd(chosen: Mapping[PeriodKey, Value], fy: int, k: int) -> Value | None:
    if k == 1:
        return chosen.get(("Q", fy, 1))
    if k == 4:
        return chosen.get(("FY", fy, 0))
    return chosen.get(("YTD", fy, k))


def _fill_quarters_from_ytd(concept: str, chosen: dict[PeriodKey, Value]) -> None:
    """Derive missing quarters as YTD(q) - YTD(q-1) across candidate tags.

    Tag-level derivation (periods.py) cannot bridge a tag switch, e.g. MAR's FY2025 capex is
    tagged PaymentsToAcquireProductiveAssets in the 10-K but PaymentsToAcquirePropertyPlant...
    in the 10-Qs. Both are candidates for the same concept, so their difference is valid.
    """
    years = {fy for (ptype, fy, _) in chosen if ptype in ("FY", "YTD")}
    for fy in sorted(years):
        for fq in (2, 3, 4):
            if ("Q", fy, fq) in chosen:
                continue
            cur, prev = _ytd(chosen, fy, fq), _ytd(chosen, fy, fq - 1)
            if cur is None or prev is None or cur.value is None or prev.value is None:
                continue
            latest = max((cur, prev), key=lambda v: (v.filed, v.accn))
            chosen[("Q", fy, fq)] = Value(
                value=cur.value - prev.value,
                status="OK",
                derivation="DERIVED",
                period_end=cur.period_end,
                accn=latest.accn,
                filed=latest.filed,
                formula=f"{cur.formula} - {prev.formula}",
                sources=tuple(dict.fromkeys((*cur.sources, *prev.sources))),
            )


_BINOPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_CMPOPS: dict[type[ast.cmpop], Callable[[float, float], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
}


class _Undefined(Exception):
    """Expression has no value (division by zero)."""


def _eval(node: ast.AST, env: Mapping[str, float]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, env)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Div) and right == 0:
            raise _Undefined
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMPOPS:
        return _CMPOPS[type(node.ops[0])](_eval(node.left, env), _eval(node.comparators[0], env))
    raise ValueError(f"unsupported expression: {ast.dump(node)}")


def _names(expr: str) -> list[str]:
    return [n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)]


def _combine(parts: list[Value], value: float | None, status: str, formula: str) -> Value:
    latest = max(parts, key=lambda v: (v.filed, v.accn))
    single = len(parts) == 1 and formula == parts[0].formula
    sources = tuple(dict.fromkeys(s for p in parts for s in p.sources))
    return Value(
        value=value,
        status=status,
        derivation=parts[0].derivation if single else "COMPUTED",
        period_end=max(p.period_end for p in parts),
        accn=latest.accn,
        filed=latest.filed,
        formula=formula,
        sources=sources,
    )


PRIOR_YEAR = "_py"  # `assets_py`: assets for the same period a year earlier (averages, D-043)


def _lookup(
    name: str, key: PeriodKey, known: Mapping[str, Mapping[PeriodKey, Value]]
) -> Value | None:
    if name.endswith(PRIOR_YEAR):
        return known.get(name[: -len(PRIOR_YEAR)], {}).get((key[0], key[1] - 1, key[2]))
    return known.get(name, {}).get(key)


def _formula_value(
    metric: MetricDef, key: PeriodKey, known: Mapping[str, Mapping[PeriodKey, Value]]
) -> Value | None:
    for expr in metric.formula:
        names = _names(expr)
        present = {n: v for n in names if (v := _lookup(n, key, known)) is not None}
        required = [n for n in names if n not in metric.optional]
        if not required or any(n not in present for n in required):
            continue
        parts = list(present.values())
        if any(p.value is None for p in parts):
            continue  # an input is itself N/M
        env = {n: 0.0 for n in names} | {
            n: v.value for n, v in present.items() if v.value is not None
        }
        if metric.nm_when:
            nm_names = _names(metric.nm_when)
            if all(key in known.get(n, {}) and known[n][key].value is not None for n in nm_names):
                nm_env = {n: known[n][key].value for n in nm_names}
                if _eval(ast.parse(metric.nm_when, mode="eval"), nm_env):  # type: ignore[arg-type]
                    return _combine(parts, None, "NM", expr)
        try:
            value = _eval(ast.parse(expr, mode="eval"), env)
        except _Undefined:
            continue
        label = parts[0].formula if len(names) == 1 and len(parts) == 1 else expr
        return _combine(parts, float(value), "OK", label)
    return None


def _comparison_key(key: PeriodKey, compare: str) -> PeriodKey:
    ptype, fy, fq = key
    if compare == "prior_year":
        return (ptype, fy - 1, fq)
    return ("Q", fy - 1, 4) if fq == 1 else ("Q", fy, fq - 1)


def _growth_value(
    metric: MetricDef, key: PeriodKey, known: Mapping[str, Mapping[PeriodKey, Value]]
) -> Value | None:
    assert metric.base and metric.compare
    series = known.get(metric.base, {})
    cur, prev = series.get(key), series.get(_comparison_key(key, metric.compare))
    if cur is None or prev is None or cur.value is None or prev.value is None or prev.value == 0:
        return None
    growth = (cur.value - prev.value) / abs(prev.value)
    return _combine([cur, prev], growth, "OK", f"growth({metric.base}, {metric.compare})")


def compute_metrics(
    periods: pd.DataFrame, registry: CompanyRegistry
) -> dict[str, dict[PeriodKey, Value]]:
    """All applicable metrics for one company, keyed by metric code then period."""
    known: dict[str, dict[PeriodKey, Value]] = dict(concept_values(periods, registry))
    results: dict[str, dict[PeriodKey, Value]] = {}
    for metric in registry.metrics:
        if metric.kind == "text":
            continue  # extracted from filing text by the LLM stage, not computed here
        inputs = metric.inputs()
        keys = sorted({k for n in inputs for k in known.get(n, {})})
        values: dict[PeriodKey, Value] = {}
        for key in keys:
            if key[0] not in metric.period_types:
                continue
            v = (
                _growth_value(metric, key, known)
                if metric.kind == "growth"
                else _formula_value(metric, key, known)
            )
            if v is not None:
                values[key] = v
        results[metric.code] = values
        known[metric.code] = values  # later metrics may reference this one
    return results
