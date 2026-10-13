"""Concept map + metric registry (config/concept_map.yaml, metrics.yaml, company_overrides/)."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from uw_pipeline.config import IndustryGroup

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


class ConceptDef(BaseModel):
    tags: list[TagRef]
    by_group: dict[str, list[TagRef]] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: list[Any]) -> list[TagRef]:
        return _tag_refs(v)

    @field_validator("by_group", mode="before")
    @classmethod
    def _groups(cls, v: dict[str, list[Any]]) -> dict[str, list[TagRef]]:
        return {k: _tag_refs(x) for k, x in v.items()}


class MetricDef(BaseModel):
    code: str
    display_name: str
    description: str | None = None
    section: str
    unit: Literal["USD", "ratio", "fraction"]
    kind: Literal["formula", "growth"] = "formula"
    formula: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    nm_when: str | None = None
    base: str | None = None
    compare: Literal["prior_quarter", "prior_year"] | None = None
    period_types: list[PeriodType] = Field(default_factory=lambda: list(ALL_PERIOD_TYPES))
    display: bool = True
    company_specific: bool = False

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
        elif not (self.base and self.compare):
            raise ValueError(f"{self.code}: growth metric needs `base` and `compare`")
        return self

    def inputs(self) -> set[str]:
        if self.kind == "growth":
            return {self.base} if self.base else set()
        exprs = [*self.formula, *([self.nm_when] if self.nm_when else [])]
        return {
            n.id
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
            unknown_codes = set(g.metrics) - codes
            if unknown_sets or unknown_codes:
                raise ValueError(
                    f"industry group {g.code}: unknown metric sets {sorted(unknown_sets)} "
                    f"/ metrics {sorted(unknown_codes)}"
                )

    def for_company(self, ticker: str, group: IndustryGroup) -> CompanyRegistry:
        override = self.overrides.get(ticker, CompanyOverride())
        concepts: dict[str, list[TagRef]] = {}
        for code, c in (self.concepts | override.concepts).items():
            concepts[code] = c.by_group.get(group.code, c.tags)

        catalog = self._catalog(ticker)
        by_code = {m.code: m for m in catalog}
        wanted = {c for s in group.metric_sets for c in self.metric_sets[s]}
        wanted |= set(group.metrics) | {m.code for m in override.metrics}
        # Pull in helper metrics that wanted metrics depend on.
        stack = list(wanted)
        while stack:
            for dep in by_code[stack.pop()].inputs() & by_code.keys():
                if dep not in wanted:
                    wanted.add(dep)
                    stack.append(dep)
        metrics = [m for m in catalog if m.code in wanted]  # catalog order = dependency order
        return CompanyRegistry(concepts=concepts, metrics=metrics)


class CompanyRegistry(BaseModel):
    """Effective concepts (tag candidates) and applicable metrics for one company."""

    concepts: dict[str, list[TagRef]]
    metrics: list[MetricDef]

    def tag_refs(self) -> set[TagRef]:
        return {t for tags in self.concepts.values() for t in tags}


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        }
    )
