"""Typed loader for config/signal_rules.yaml."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

from uw_pipeline.yaml_load import safe_load

if TYPE_CHECKING:
    from uw_pipeline.config import IndustryGroup

Severity = Literal["Info", "Warning", "Critical"]


class Downgrade(BaseModel):
    pattern: str
    severity: Severity
    subcategory: str
    title: str


class SignalSpec(BaseModel):
    category: str
    subcategory: str
    severity: Severity
    title: str
    downgrade: Downgrade | None = None


class LeadershipRules(BaseModel):
    roles: dict[str, str]
    exclude: str
    own_entity: str
    proximity: int = 100
    departure: str
    appointment: str
    severity: dict[Literal["departure", "appointment", "succession"], Severity]

    @field_validator("departure", "appointment", "exclude", "own_entity")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v


class KeywordRule(SignalSpec):
    id: str
    sections: list[str]
    pattern: str
    exclude: str | None = None
    industries: list[str] | None = None  # only these industry groups (default: all), D-049

    @field_validator("pattern", "exclude")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class BuybackRule(SignalSpec):
    min_buybacks_ttm: float = 0


class PayRule(SignalSpec):
    min_pay_growth: float = 0.25  # CEO total compensation, year on year


class IndependenceRule(SignalSpec):
    min_ratio: float = 2 / 3  # independent directors / board size


class NonAuditFeeRule(SignalSpec):
    min_share: float = 0.5  # non-audit fees / total fees paid to the auditor


class ThresholdRule(SignalSpec):
    """A metric crossing a threshold in its latest period (D-049). Values in stored units:
    fraction 0.05 = 5%, ratio 1.2 = 1.2x, USD."""

    id: str
    metric: str  # metric code
    op: Literal["<", "<=", ">", ">="]
    value: float
    period: Literal["Q", "FY"] = "Q"  # latest quarter or latest fiscal year
    industries: list[str] | None = None  # only these industry groups (default: all)


class DropRule(SignalSpec):
    min_drop: float = 0.10  # the day's return at most -min_drop
    min_excess: float = 0.08  # and at least this far below the benchmark's return


class MissRule(SignalSpec):
    days: int = 3  # trading days after the earnings report
    min_drop: float = 0.08  # cumulative fall from the close before the report


class ClusterRule(SignalSpec):
    min_insiders: int = 3
    window_days: int = 30
    escalate_days: int = 90  # Warning when a stock drop or earnings-miss drop follows
    lookback_days: int = 1095


class InsiderRules(BaseModel):
    """Insider-selling signals (Phase 15b)."""

    insider_cluster_selling: ClusterRule | None = None


class MarketRules(BaseModel):
    """Market signals (Phase 15a)."""

    benchmark: str = "SPY"
    lookback_days: int = 1095
    stock_drop: DropRule | None = None
    earnings_miss_drop: MissRule | None = None


LEGAL_TYPES = ("securities_class_action", "derivative", "regulatory_investigation", "enforcement",
               "wells_notice")  # fmt: skip


class LegalRules(BaseModel):
    """Litigation and regulatory matter signals (Phase 15e), by matter type; rule ids
    legal_<type>. Product-liability, antitrust and other matters reach insights, not signals."""

    lookback_days: int = 730  # began (or, with no date given, is open) within this window
    active_days: int = 400  # still disclosed in a filing within this window
    securities_class_action: SignalSpec | None = None
    derivative: SignalSpec | None = None
    regulatory_investigation: SignalSpec | None = None
    enforcement: SignalSpec | None = None
    wells_notice: SignalSpec | None = None  # an investigation with a Wells notice


class MetricRules(BaseModel):
    negative_equity: SignalSpec
    buybacks_with_negative_equity: BuybackRule
    pay_vs_performance: PayRule | None = None
    # Board composition from the proxy (D-045).
    ceo_chair_without_lead_director: SignalSpec | None = None
    board_independence: IndependenceRule | None = None
    dual_class_shares: SignalSpec | None = None  # Phase 15f
    non_audit_fees: NonAuditFeeRule | None = None  # Phase 15f


class SignalRules(BaseModel):
    eight_k_items: dict[str, SignalSpec]
    fetch_body_items: list[str] = Field(default_factory=list)
    leadership: LeadershipRules
    keywords: list[KeywordRule]
    metrics: MetricRules
    thresholds: list[ThresholdRule] = Field(default_factory=list)
    ownership_forms: dict[str, SignalSpec] = Field(default_factory=dict)  # 13D (Phase 15d)
    market: MarketRules = Field(default_factory=MarketRules)  # Phase 15a
    insiders: InsiderRules = Field(default_factory=InsiderRules)  # Phase 15b
    legal: LegalRules = Field(default_factory=LegalRules)  # Phase 15e

    def rule_ids(self) -> set[str]:
        """Ids an industry can exclude or parameterize. `8k_5.02` also covers the role-specific
        ids it produces (8k_5.02_cfo_departure, ...)."""
        metric = {k for k, v in self.metrics if v is not None}
        return (
            {f"8k_{item}" for item in self.eight_k_items}
            | {"auditor", "auditor_change"}
            | {k.id for k in self.keywords}
            | metric
            | {t.id for t in self.thresholds}
            | {ownership_rule_id(f) for f in self.ownership_forms}
            | {k for k in ("stock_drop", "earnings_miss_drop") if getattr(self.market, k)}
            | ({"insider_cluster_selling"} if self.insiders.insider_cluster_selling else set())
            | {f"legal_{t}" for t in LEGAL_TYPES if getattr(self.legal, t)}
        )

    def _scoped(self) -> dict[str, list[str]]:
        """Rules limited to some industries: id -> industry groups."""
        out = {r.id: r.industries for r in self.keywords if r.industries}
        out |= {r.id: r.industries for r in self.thresholds if r.industries}
        return out

    def allows(self, rule_id: str, group: IndustryGroup | None) -> bool:
        """Whether a detected signal is tracked for companies of this group (D-049)."""
        if group is None:
            return True
        ids = self.rule_ids()
        family = next((r for r in [rule_id, *_families(rule_id)] if r in ids), rule_id)
        scope = self._scoped().get(family)
        if scope is not None and group.code not in scope:
            return False
        return family not in group.exclude_signals

    def for_group(self, group: IndustryGroup | None) -> SignalRules:
        """The rules with the group's `signal_params` applied."""
        if group is None or not group.signal_params:
            return self
        data = self.model_dump()
        for rule_id, params in group.signal_params.items():
            _spec_of(data, rule_id).update(params)
        return SignalRules.model_validate(data)

    def check_groups(self, groups: Iterable[IndustryGroup]) -> None:
        """Excluded and parameterized signals exist, with real fields; rules limited to
        industries name existing groups."""
        groups = list(groups)
        ids, codes = self.rule_ids(), {g.code for g in groups}
        for rule_id, scope in self._scoped().items():
            unknown_groups = set(scope) - codes
            if unknown_groups:
                raise ValueError(f"signal {rule_id}: unknown industries {sorted(unknown_groups)}")
        for g in groups:
            unknown = (set(g.exclude_signals) | set(g.signal_params)) - ids
            if unknown:
                raise ValueError(
                    f"industry group {g.code}: unknown signals {sorted(unknown)}; "
                    f"known: {sorted(ids)}"
                )
            data = self.model_dump()
            for rule_id, params in g.signal_params.items():
                spec = _spec_of(data, rule_id)
                extra = set(params) - set(spec)
                if extra:
                    raise ValueError(
                        f"industry group {g.code}: signal {rule_id} has no parameter(s) "
                        f"{sorted(extra)}; it has {sorted(spec)}"
                    )
            self.for_group(g)  # parameter values must validate


def ownership_rule_id(form: str) -> str:
    """`SCHEDULE 13D` / `SC 13D` -> 13d; amendments -> 13d_amendment."""
    base = form.upper().replace("SCHEDULE ", "").replace("SC ", "")
    return base.removesuffix("/A").lower() + ("_amendment" if base.endswith("/A") else "")


def _families(rule_id: str) -> list[str]:
    """`8k_5.02_cfo_departure` -> [`8k_5.02_cfo`, `8k_5.02`, `8k`]."""
    parts = rule_id.split("_")
    return ["_".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def _spec_of(data: dict[str, Any], rule_id: str) -> dict[str, Any]:
    """The dumped spec of a rule id (8-K item, keyword, metric or threshold rule)."""
    if rule_id.startswith("8k_") and rule_id[3:] in data["eight_k_items"]:
        spec: dict[str, Any] = data["eight_k_items"][rule_id[3:]]
        return spec
    for r in [*data["keywords"], *data["thresholds"]]:
        if r["id"] == rule_id:
            found: dict[str, Any] = r
            return found
    if rule_id in ("stock_drop", "earnings_miss_drop") and data["market"].get(rule_id):
        market: dict[str, Any] = data["market"][rule_id]
        return market
    if rule_id == "insider_cluster_selling" and data["insiders"].get(rule_id):
        insiders: dict[str, Any] = data["insiders"][rule_id]
        return insiders
    if rule_id.startswith("legal_") and data["legal"].get(rule_id[6:]):
        legal: dict[str, Any] = data["legal"][rule_id[6:]]
        return legal
    if data["metrics"].get(rule_id) is not None:
        spec = data["metrics"][rule_id]
        return spec
    raise ValueError(f"signal {rule_id} has no parameters to set")


def load_signal_rules(config_dir: Path) -> SignalRules:
    with (config_dir / "signal_rules.yaml").open(encoding="utf-8") as f:
        return SignalRules.model_validate(safe_load(f))
