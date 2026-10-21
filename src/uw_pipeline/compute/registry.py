"""Concept map + metric registry (config/concept_map.yaml, metrics.yaml, company_overrides/)."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from uw_pipeline.config import IndustryGroup
from uw_pipeline.yaml_load import safe_load

# Taxonomies SEC includes in companyfacts. Anything else (company extensions) needs the
# filing's XBRL instance.
COMPANYFACTS_TAXONOMIES = frozenset({"us-gaap", "dei", "srt", "ifrs-full", "invest"})

PeriodType = Literal["Q", "FY"]
ALL_PERIOD_TYPES: tuple[PeriodType, ...] = ("Q", "FY")


class TagRef(BaseModel, frozen=True):
    """One XBRL fact selector: a prefixed tag plus optional explicit dimension members."""

    tag: str
    dims: tuple[tuple[str, str], ...] = ()

    @field_validator("tag")
    @classmethod
    def _prefixed(cls, v: str) -> str:
        if v.count(":") != 1:
            raise ValueError(f"tag must be 'taxonomy:Name', got {v!r}")
        return v

    @field_validator("dims", mode="before")
    @classmethod
    def _dims(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return tuple(sorted(v.items()))
        return v

    @property
    def taxonomy(self) -> str:
        return self.tag.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.tag.split(":", 1)[1]

    @property
    def dims_key(self) -> str:
        return dims_key(self.dims)

    @property
    def needs_instance(self) -> bool:
        return bool(self.dims) or self.taxonomy not in COMPANYFACTS_TAXONOMIES

    def label(self) -> str:
        return self.tag + (f"[{self.dims_key}]" if self.dims else "")


def dims_key(dims: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> str:
    """Canonical string for a set of explicit dimension members ('' = no dimensions)."""
    return "|".join(f"{a}={m}" for a, m in sorted(dims))


def _tag_refs(raw: list[Any]) -> list[TagRef]:
    return [TagRef(tag=x) if isinstance(x, str) else TagRef.model_validate(x) for x in raw]


# Regulatory floors and buffers are tagged with the same unit, period and dimensions as the
# actual capital ratios; a concept must never pick one up (decisions D-042).
_THRESHOLD = re.compile(r"Minimum|Required|WellCapitalized|Buffer|Surcharge")


class ConceptDef(BaseModel):
    tags: list[TagRef]
    by_group: dict[str, list[TagRef]] = Field(default_factory=dict)
    unit: str = "USD"  # unit of the facts to use: USD, or pure for ratios

    @model_validator(mode="after")
    def _no_thresholds(self) -> ConceptDef:
        if self.unit == "pure":
            refs = [*self.tags, *(r for rs in self.by_group.values() for r in rs)]
            bad = [r.tag for r in refs if _THRESHOLD.search(r.name)]
            if bad:
                raise ValueError(f"regulatory threshold tags are not actual ratios: {bad}")
        return self

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: list[Any]) -> list[TagRef]:
        return _tag_refs(v)

    @field_validator("by_group", mode="before")
    @classmethod
    def _groups(cls, v: dict[str, list[Any]]) -> dict[str, list[TagRef]]:
        return {k: _tag_refs(x) for k, x in v.items()}


class TableSpec(BaseModel):
    """Read from a table by cell coordinates and checked mechanically (decisions D-044)."""

    column: list[str]  # the metric: in the column's header or the row's label, e.g. [RevPAR]
    find: list[str] = Field(default_factory=list)  # words that pick the tables sent (default:
    #   `column`); narrower when `column` is a common word such as Revenue
    scope: list[list[str]] = Field(default_factory=list)  # each group: one word in the row's
    #   label, the group heading above it or the table title, e.g. [[systemwide]]
    exclude: list[str] = Field(default_factory=lambda: ["vs.", "vs ", "change", "variance"])
    exclude_rows: list[str] = Field(default_factory=list)  # a subset (region, brand, ...) in
    #   the row's label or its group heading
    exclude_tables: list[str] = Field(  # not reported figures, wherever the table says so
        default_factory=lambda: ["illustrative", "hypothetical", "example", "pro forma",
                                 "sensitivity", "not reported"])  # fmt: skip


class TextExtraction(BaseModel):
    """How an LLM finds a `kind: text` metric in filing text (Phase 5a)."""

    definition: str  # exact scope, e.g. "comparable systemwide worldwide RevPAR"
    keywords: list[str]  # used to pick the relevant text chunks
    dual_check: bool = False  # extractor and tie-breaker models must agree
    table: TableSpec | None = None  # read from tables by cell coordinates instead of quotes


class MetricDef(BaseModel):
    code: str
    display_name: str
    description: str | None = None
    section: str
    # ounces, pounds, tons: production and shipment volumes (Phase 17, D-055); shown and given
    # year-over-year growth like other metrics.
    unit: Literal[
        "USD",
        "ratio",
        "fraction",
        "days",
        "shares",
        "score",
        "count",
        "ounces",
        "pounds",
        "tons",
        "months",
    ]
    kind: Literal["formula", "growth", "text", "external"] = "formula"  # external: Phase 15
    formula: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    nm_when: str | None = None
    base: str | None = None
    compare: Literal["prior_quarter", "prior_year"] | None = None
    period_types: list[PeriodType] = Field(default_factory=lambda: list(ALL_PERIOD_TYPES))
    display: bool = True
    insight_input: bool = False  # not displayed, but part of the insight input (e.g. CEO pay)
    company_specific: bool = False
    extraction: TextExtraction | None = None

    @field_validator("formula", mode="before")
    @classmethod
    def _formula_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> MetricDef:
        if self.kind == "formula":
            if not self.formula:
                raise ValueError(f"{self.code}: formula metric needs `formula`")
            for f in [*self.formula, *([self.nm_when] if self.nm_when else [])]:
                ast.parse(f, mode="eval")
        elif self.kind == "text":
            if self.extraction is None:
                raise ValueError(f"{self.code}: text metric needs `extraction`")
        elif self.kind == "external":
            pass  # written by another stage: market (prices, EPS) or documents (Form 4)
        elif not (self.base and self.compare):
            raise ValueError(f"{self.code}: growth metric needs `base` and `compare`")
        return self

    def inputs(self) -> set[str]:
        if self.kind in ("text", "external"):
            return set()
        if self.kind == "growth":
            return {self.base} if self.base else set()
        exprs = [*self.formula, *([self.nm_when] if self.nm_when else [])]
        return {
            # `x_py`: x a year earlier; `x_ttm`: the last four quarters (compute/metrics.py)
            n.id.removesuffix("_py").removesuffix("_ttm")
            for e in exprs
            for n in ast.walk(ast.parse(e, mode="eval"))
            if isinstance(n, ast.Name)
        }


class SectionRule(BaseModel):
    """Locate a section by its own heading lines when item headings don't work (e.g. JPM)."""

    start: str
    end: str


class CompanyOverride(BaseModel):
    concepts: dict[str, ConceptDef] = Field(default_factory=dict)
    metrics: list[MetricDef] = Field(default_factory=list)
    sections: dict[str, SectionRule] = Field(default_factory=dict)


class Registry(BaseModel):
    concepts: dict[str, ConceptDef]
    metrics: list[MetricDef]
    metric_sets: dict[str, list[str]] = Field(default_factory=dict)
    overrides: dict[str, CompanyOverride] = Field(default_factory=dict)
    # Metric sets computed and stored but not shown: not published to the app, not in any LLM
    # input, no threshold signals, not in coverage (D-054). Removing a set shows it again.
    hidden_metric_sets: list[str] = Field(default_factory=list)

    @property
    def hidden_codes(self) -> frozenset[str]:
        return frozenset(c for s in self.hidden_metric_sets for c in self.metric_sets[s])

    @model_validator(mode="after")
    def _check_refs(self) -> Registry:
        for ticker in ["", *self.overrides]:
            concepts = self.concepts | (self.overrides[ticker].concepts if ticker else {})
            known: set[str] = set(concepts)
            for m in self._catalog(ticker):
                missing = m.inputs() - known
                if missing:
                    where = f" (override {ticker})" if ticker else ""
                    raise ValueError(f"metric {m.code}{where} references unknown {sorted(missing)}")
                known.add(m.code)
        codes = {m.code for m in self.metrics}
        for name, members in self.metric_sets.items():
            unknown = set(members) - codes
            if unknown:
                raise ValueError(f"metric set {name} lists unknown metrics {sorted(unknown)}")
        unknown_sets = set(self.hidden_metric_sets) - self.metric_sets.keys()
        if unknown_sets:
            raise ValueError(f"hidden_metric_sets: unknown metric sets {sorted(unknown_sets)}")
        return self

    def _catalog(self, ticker: str) -> list[MetricDef]:
        extra = self.overrides[ticker].metrics if ticker in self.overrides else []
        return [*self.metrics, *(m.model_copy(update={"company_specific": True}) for m in extra)]

    def section_rules(self, ticker: str) -> dict[str, SectionRule]:
        return self.overrides[ticker].sections if ticker in self.overrides else {}

    def check_groups(self, groups: Iterable[IndustryGroup]) -> None:
        codes = {m.code for m in self.metrics}
        for g in groups:
            unknown_sets = set(g.metric_sets) - self.metric_sets.keys()
            unknown_codes = (set(g.metrics) | set(g.exclude_metrics)) - codes
            if unknown_sets or unknown_codes:
                raise ValueError(
                    f"industry group {g.code}: unknown metric sets {sorted(unknown_sets)} "
                    f"/ metrics {sorted(unknown_codes)}"
                )

    def for_company(self, ticker: str, group: IndustryGroup) -> CompanyRegistry:
        override = self.overrides.get(ticker, CompanyOverride())
        concepts: dict[str, list[TagRef]] = {}
        units: dict[str, str] = {}
        for code, c in (self.concepts | override.concepts).items():
            concepts[code] = c.by_group.get(group.code, c.tags)
            units[code] = c.unit

        catalog = self._catalog(ticker)
        by_code = {m.code: m for m in catalog}
        wanted = {c for s in group.metric_sets for c in self.metric_sets[s]}
        wanted |= set(group.metrics) | {m.code for m in override.metrics}
        wanted -= set(group.exclude_metrics)  # not tracked for this industry (D-049)
        tracked = set(wanted)
        # Pull in helper metrics that wanted metrics depend on.
        stack = list(wanted)
        while stack:
            for dep in by_code[stack.pop()].inputs() & by_code.keys():
                if dep not in wanted:
                    wanted.add(dep)
                    stack.append(dep)
        hidden = self.hidden_codes
        metrics = [  # catalog order = dependency order
            m.model_copy(update={"display": False, "insight_input": False})
            # removed by the industry but needed by another metric (a hidden helper), or in a
            # hidden set (computed, not shown, D-054)
            if (m.code in group.exclude_metrics and m.code not in tracked) or m.code in hidden
            else m
            for m in catalog
            if m.code in wanted
        ]
        # A concept read from XBRL instances (dimensional or company tags) only when one of
        # the company's metrics uses it: otherwise every company would read an instance per
        # filing for, say, casino revenue (Phase 17). Other concepts stay, as stages other
        # than compute read their facts.
        used = {n for m in metrics for n in m.inputs()}
        concepts = {code: tags for code, tags in concepts.items()
                    if code in used or not any(t.needs_instance for t in tags)}  # fmt: skip
        return CompanyRegistry(concepts=concepts, units=units, metrics=metrics)


class CompanyRegistry(BaseModel):
    """Effective concepts (tag candidates) and applicable metrics for one company."""

    concepts: dict[str, list[TagRef]]
    units: dict[str, str] = Field(default_factory=dict)  # concept -> fact unit (default USD)
    metrics: list[MetricDef]

    def tag_refs(self) -> set[TagRef]:
        return {t for tags in self.concepts.values() for t in tags}


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return safe_load(f)


def load_registry(config_dir: Path) -> Registry:
    concepts = _load(config_dir / "concept_map.yaml")["concepts"]
    catalog = _load(config_dir / "metrics.yaml")
    overrides = {
        p.stem.upper(): _load(p) for p in sorted((config_dir / "company_overrides").glob("*.yaml"))
    }
    return Registry.model_validate(
        {
            "concepts": concepts,
            "metrics": catalog["metrics"],
            "metric_sets": catalog.get("metric_sets", {}),
            "overrides": overrides,
            "hidden_metric_sets": catalog.get("hidden_metric_sets", []),
        }
    )
