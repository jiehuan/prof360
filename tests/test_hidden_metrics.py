"""Hidden metric sets (D-054): the Phase 15 metrics beyond the BRD are computed and stored but not
published to the app, not given to the LLM, and raise no threshold signals."""

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select

from tests import test_insights as ti
from tests import test_publish as tp
from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import Registry, load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import load_insights_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.jobs.run_insights import run_insights_stage
from uw_pipeline.jobs.run_publish import load_publish_config, run_publish_stage
from uw_pipeline.schema import company, metric_value
from uw_pipeline.schema_app import MatrixData, SIC_Preferences, metrics
from uw_pipeline.signals.detect import threshold_signals
from uw_pipeline.signals.rules import load_signal_rules

REG = load_registry(CONFIG_DIR)
SHOWN = REG.model_copy(update={"hidden_metric_sets": []})
HOTELS = load_industry_config(CONFIG_DIR).groups["HOTELS"]


def test_hidden_sets_are_computed_but_not_shown() -> None:
    hidden = {"market_cap", "altman_z", "goodwill_to_equity", "receivables_days"}
    assert hidden <= REG.hidden_codes
    mar = {m.code: m for m in REG.for_company("MAR", HOTELS).metrics}
    for code in hidden:  # still in the company's list, so compute and the market stage write it
        assert not mar[code].display and not mar[code].insight_input
    assert mar["revenue"].display and mar["total_debt"].display  # the BRD metrics stay
    shown = {m.code: m for m in SHOWN.for_company("MAR", HOTELS).metrics}
    assert all(shown[c].display for c in hidden)  # removing the list shows them again


def test_unknown_hidden_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="hidden_metric_sets"):
        Registry.model_validate(REG.model_dump() | {"hidden_metric_sets": ["NOPE"]})


def test_no_threshold_signal_on_a_hidden_metric() -> None:
    rules = load_signal_rules(CONFIG_DIR)
    row = {"metric_code": "altman_z", "period_type": "FY", "fiscal_year": 2025,
           "fiscal_quarter": 0, "value": 1.2, "period_end": "2025-12-31",
           "source_accn": "a"}  # fmt: skip
    assert [s.rule_id for s in threshold_signals([row], rules)] == ["altman_distress"]
    assert threshold_signals([row], rules, REG.hidden_codes) == []


def _market_cap(engine) -> None:
    with engine.begin() as c:
        c.execute(insert(metric_value).values(
            company_id=1, metric_code="market_cap", period_type="Q", fiscal_year=2026,
            fiscal_quarter=2, period_end="2026-06-30", value=7.7e10, value_status="OK", unit="USD",
            derivation="MARKET", formula="f", source_tags="{}", source_accn="a",
            computed_at=ti.NOW))  # fmt: skip


def test_hidden_metrics_are_not_given_to_the_llm() -> None:
    def prompts(registry) -> str:
        from sqlalchemy import create_engine

        from uw_pipeline.schema import metadata

        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        ti._seed(engine)
        _market_cap(engine)
        seen: list[str] = []
        llm = FakeLLM(lambda model, user: seen.append(user) or ti.answer(model, user))
        run_insights_stage(
            [CompanyConfig(ticker="MAR", cik=1048286)], registry,
            load_industry_config(CONFIG_DIR), (llm, load_llm_config(CONFIG_DIR)),
            load_insights_config(CONFIG_DIR), load_source_labels(CONFIG_DIR), engine,
            now=lambda: ti.NOW,
        )  # fmt: skip
        return "\n".join(seen)

    assert "Market Cap" in prompts(SHOWN)  # the test can see it when shown
    assert "Market Cap" not in prompts(REG)


def test_hidden_metrics_leave_the_app() -> None:
    engine, app, _ = tp._env()
    _market_cap(engine)
    with engine.begin() as c:  # a SIC code, so the SIC's metric list is published
        c.execute(company.update().values(sic_code="7011", sic_description="Hotels & Motels"))

    def publish(registry) -> None:
        res = run_publish_stage(
            [CompanyConfig(ticker="MAR", cik=1048286)], registry,
            load_industry_config(CONFIG_DIR), load_publish_config(CONFIG_DIR),
            load_source_labels(CONFIG_DIR), engine, app, now=lambda: ti.NOW,
        )  # fmt: skip
        assert res[0].ok, res

    def counts() -> tuple[int, int]:
        with app.connect() as c:
            name = metrics.c.metric_name == "Market Capitalization"
            mid = c.execute(select(metrics.c.metric_id).where(name)).scalar()
            if mid is None:
                return 0, 0
            return (
                c.execute(select(func.count()).select_from(MatrixData)
                          .where(MatrixData.c.metric_id == mid)).scalar_one(),
                c.execute(select(func.count()).select_from(SIC_Preferences)
                          .where(SIC_Preferences.c.metric_id == mid)).scalar_one(),
            )  # fmt: skip

    publish(SHOWN)
    assert counts() == (1, 1)
    publish(REG)  # hidden: its values and its place in the SIC's metric list are removed
    assert counts() == (0, 0)
