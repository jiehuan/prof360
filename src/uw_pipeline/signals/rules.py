"""Typed loader for config/signal_rules.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

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
    severity: dict[Literal["departure", "appointment"], Severity]

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

    @field_validator("pattern", "exclude")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class BuybackRule(SignalSpec):
    min_buybacks_ttm: float = 0


class MetricRules(BaseModel):
    negative_equity: SignalSpec
    buybacks_with_negative_equity: BuybackRule


class SignalRules(BaseModel):
    eight_k_items: dict[str, SignalSpec]
    fetch_body_items: list[str] = Field(default_factory=list)
    leadership: LeadershipRules
    keywords: list[KeywordRule]
    metrics: MetricRules


def load_signal_rules(config_dir: Path) -> SignalRules:
    with (config_dir / "signal_rules.yaml").open(encoding="utf-8") as f:
        return SignalRules.model_validate(yaml.safe_load(f))
