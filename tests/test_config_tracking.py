"""What a company and an industry track, set by editing config/ (D-049): a company's industry
in companies.yaml, metrics and signals an industry excludes, signal parameters per industry,
signals limited to industries, and metric threshold signals."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from tests.conftest import CONFIG_DIR, TEST_UA
from uw_pipeline.cli import app
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.signals.detect import threshold_signals
from uw_pipeline.signals.rules import SignalRules, ThresholdRule, load_signal_rules

LUV_SIC = 4512
Edit = Callable[[dict[str, Any]], None]


def edited(tmp_path: Path, **edits: Edit) -> Path:
    """A copy of config/ with `edits` applied: file stem -> function changing its YAML."""
    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config)
    for stem, edit in edits.items():
        path = config / f"{stem}.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        edit(raw)
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return config


def luv_to_hotels(raw: dict[str, Any]) -> None:
    luv = next(c for c in raw["companies"] if c["ticker"] == "LUV")
    luv |= {"industry": "HOTELS", "industry_reason": "test: a travel company"}


def hotels_track(raw: dict[str, Any]) -> None:
    hotels = raw["groups"]["HOTELS"]
    hotels["metrics"] = [*hotels.get("metrics", []), "debt_to_revenue"]
    hotels["exclude_metrics"] = ["operating_income", "buyback_intensity"]
    hotels["exclude_signals"] = ["8k_5.02"]
    hotels["signal_params"] = {"pay_vs_performance": {"min_pay_growth": 0.5}}
    raw["groups"]["CRUISE"] = {"label": "Cruise Lines", "is_financial": False,
                               "metric_sets": ["COMMERCIAL_CORE"]}  # fmt: skip
    raw["sic"][4400] = "CRUISE"


def new_metric(raw: dict[str, Any]) -> None:
    raw["metrics"].append({"code": "debt_to_revenue", "display_name": "Total Debt / Revenue",
                           "section": "leverage", "unit": "ratio",
                           "formula": "total_debt / revenue"})  # fmt: skip


def new_signals(raw: dict[str, Any]) -> None:
    raw["keywords"].append(
        {
            "id": "shrink_language",
            "category": "Risk & Legal",
            "subcategory": "Inventory Shrink",
            "severity": "Warning",
            "title": "Shrink mentioned",
            "sections": ["10K-1A"],
            "pattern": r"\bshrink\b",
            "industries": ["HOTELS"],
        }
    )
    raw["thresholds"] = [
        {
            "id": "thin_liquidity",
            "category": "Risk & Legal",
            "subcategory": "Liquidity & Leverage",
            "severity": "Warning",
            "title": "Current ratio below 1.0",
            "metric": "current_ratio",
            "op": "<",
            "value": 1.0,
        }
    ]


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return edited(tmp_path, companies=luv_to_hotels, industry_groups=hotels_track,
                  metrics=new_metric, signal_rules=new_signals)  # fmt: skip


def test_company_industry_from_companies_yaml(config: Path) -> None:
    industry = load_industry_config(config)
    luv = next(c for c in load_companies(config) if c.ticker == "LUV")
    assert industry.classify(LUV_SIC, luv)[0].code == "HOTELS"
    assert industry.classify(LUV_SIC, luv)[1] == "company"
    assert industry.classify(LUV_SIC)[0].code == "AIRLINES"  # other companies: by SIC
    cruise, how = industry.classify(4400)
    assert (cruise.code, how) == ("CRUISE", "sic") and "GOVERNANCE" in cruise.metric_sets


def test_industry_metrics(config: Path) -> None:
    industry, registry = load_industry_config(config), load_registry(config)
    registry.check_groups(industry.groups.values())
    metrics = {m.code: m for m in registry.for_company("MAR", industry.groups["HOTELS"]).metrics}
    assert metrics["debt_to_revenue"].display and "buyback_intensity" not in metrics
    # Excluded, but EBITDA needs it: computed, hidden.
    assert not metrics["operating_income"].display and metrics["ebitda_computed"].display
    airlines = {m.code for m in registry.for_company("LUV", industry.groups["AIRLINES"]).metrics}
    assert "debt_to_revenue" not in airlines and "buyback_intensity" in airlines


def test_industry_signals(config: Path) -> None:
    industry, rules = load_industry_config(config), load_signal_rules(config)
    rules.check_groups(industry.groups.values())
    hotels, airlines = industry.groups["HOTELS"], industry.groups["AIRLINES"]
    assert not rules.allows("8k_5.02_cfo_departure", hotels)  # the family is excluded
    assert rules.allows("8k_5.02_cfo_departure", airlines)
    assert rules.allows("shrink_language", hotels) and not rules.allows("shrink_language", airlines)
    assert rules.allows("thin_liquidity", airlines)  # no `industries`: every industry
    assert rules.for_group(hotels).metrics.pay_vs_performance.min_pay_growth == 0.5
    assert rules.for_group(airlines).metrics.pay_vs_performance.min_pay_growth == 0.25


def test_threshold_signal_uses_the_latest_period() -> None:
    base = load_signal_rules(CONFIG_DIR)
    rule = ThresholdRule(id="thin_liquidity", category="Risk & Legal", subcategory="Liquidity",
                         severity="Warning", title="Current ratio below 1.0",
                         metric="current_ratio", op="<", value=1.0)  # fmt: skip
    rules = SignalRules.model_validate(base.model_dump() | {"thresholds": [rule.model_dump()]})
    row = {"metric_code": "current_ratio", "period_type": "Q", "source_accn": "a"}
    values = [
        row | {"fiscal_year": 2026, "fiscal_quarter": 1, "value": 0.8, "period_end": "q1"},
        row | {"fiscal_year": 2026, "fiscal_quarter": 2, "value": 1.2, "period_end": "q2"},
    ]
    assert threshold_signals(values, rules) == []  # the latest quarter is above 1.0
    values[1]["value"] = 0.9
    [sig] = threshold_signals(values, rules)
    assert (sig.rule_id, sig.key_extra, sig.event_date) == ("thin_liquidity", "FY2026 Q2", "q2")
    values[1]["value"] = None  # N/M never fires
    assert threshold_signals(values, rules) == []


def bad(stem: str, edit: Edit) -> dict[str, Edit]:
    return {stem: edit}


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (
            bad("companies", lambda r: r["companies"][1].update(industry="HOTELS")),
            "industry_reason",
        ),
        (
            bad(
                "companies",
                lambda r: r["companies"][1].update(industry="NOPE", industry_reason="x"),
            ),
            "unknown industry group",
        ),
        (
            bad(
                "industry_groups", lambda r: r["groups"]["HOTELS"].update(exclude_metrics=["nope"])
            ),
            "unknown metric",
        ),
        (
            bad(
                "industry_groups", lambda r: r["groups"]["HOTELS"].update(exclude_signals=["nope"])
            ),
            "unknown signals",
        ),
        (
            bad(
                "industry_groups",
                lambda r: r["groups"]["HOTELS"].update(
                    signal_params={"pay_vs_performance": {"colour": 1}}
                ),
            ),
            "has no parameter",
        ),
        (
            bad(
                "industry_groups",
                lambda r: r["groups"]["HOTELS"].update(
                    signal_params={"pay_vs_performance": {"min_pay_growth": "lots"}}
                ),
            ),
            "min_pay_growth",
        ),
        (
            bad("signal_rules", lambda r: r["keywords"][0].update(industries=["NOPE"])),
            "unknown industries",
        ),
        (
            bad(
                "signal_rules",
                lambda r: r.update(
                    thresholds=[
                        {
                            "id": "t",
                            "metric": "x",
                            "op": "!=",
                            "value": 1,
                            "category": "c",
                            "subcategory": "s",
                            "severity": "Warning",
                            "title": "t",
                        }
                    ]
                ),
            ),
            "op",
        ),
    ],
)
def test_mistakes_are_reported(tmp_path: Path, edits: dict[str, Edit], message: str) -> None:
    config = edited(tmp_path, **edits)
    with pytest.raises((ValueError, ValidationError), match=message):
        companies = load_companies(config)
        industry = load_industry_config(config)
        industry.check_companies(companies)
        load_registry(config).check_groups(industry.groups.values())
        load_signal_rules(config).check_groups(industry.groups.values())


def test_config_check_and_show(config: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("SEC_USER_AGENT", TEST_UA)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'uw.db'}")
    out = CliRunner().invoke(app, ["config", "check"])
    assert out.exit_code == 0, out.output
    assert "company LUV -> HOTELS (test: a travel company)" in out.output
    assert "industry HOTELS: not tracked: metrics ['operating_income'" in out.output
    assert "signal shrink_language: only ['HOTELS']" in out.output
    show = CliRunner().invoke(app, ["config", "show", "--ticker", "LUV"])
    assert show.exit_code == 0, show.output
    assert (
        "industry: HOTELS - Hotels & Lodging  [companies.yaml: test: a travel company]"
        in show.output
    )
    assert "debt_to_revenue" in show.output and "thin_liquidity" in show.output
    assert "params {'min_pay_growth': 0.5}" in show.output
    broken = edited(
        tmp_path / "b",
        industry_groups=lambda r: r["groups"]["HOTELS"].update(exclude_signals=["nope"]),
    )
    monkeypatch.setenv("CONFIG_DIR", str(broken))
    bad_run = CliRunner().invoke(app, ["config", "check"])
    assert bad_run.exit_code == 1 and "INVALID" in bad_run.output
