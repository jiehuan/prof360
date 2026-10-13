"""Phase 9 publish stage: pipeline outputs -> application tables (stable ids, versions, links)."""

import json

from sqlalchemy import create_engine, func, insert, select, update

from tests import test_insights as ti
from tests import test_metric_detail as tm
from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.jobs.run_publish import load_publish_config, run_publish_stage
from uw_pipeline.schema import followup_question, signal
from uw_pipeline.schema_app import (
    Company,
    CompanyMetricDetail,
    FollowUpQuestions,
    ItemLink,
    KeyInsights,
    MatrixData,
    RiskTypes,
    StrategicRisk,
    app_metadata,
    metrics,
)

PCFG = load_publish_config(CONFIG_DIR)


def _publish(engine, app):
    res = run_publish_stage(
        [CompanyConfig(ticker="MAR", cik=1048286)], load_registry(CONFIG_DIR),
        load_industry_config(CONFIG_DIR), PCFG, load_source_labels(CONFIG_DIR), engine, app,
        now=lambda: ti.NOW,
    )  # fmt: skip
    assert res[0].ok, res
    return res[0].detail


def _env():
    engine = tm._env()
    llm = FakeLLM(tm.answer)
    ti._run(engine, llm)
    tm._run(engine, llm)
    app = create_engine("sqlite://")
    app_metadata.create_all(app)
    return engine, app, llm


def _count(app, table, *where):
    with app.connect() as c:
        return c.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def test_publish_maps_objects_and_links() -> None:
    engine, app, _ = _env()
    _publish(engine, app)
    with app.connect() as c:
        comp = c.execute(select(Company)).mappings().one()
        key = c.execute(select(KeyInsights)).mappings().one()
        risks = c.execute(select(StrategicRisk)).mappings().all()
        q = c.execute(select(FollowUpQuestions)).mappings().one()
        links = {(r.FromType, r.FromId, r.ToType, r.ToId) for r in c.execute(select(ItemLink))}
        debt_id = c.execute(select(metrics.c.metric_id)
                            .where(metrics.c.metric_name == "Total Debt")).scalar_one()  # fmt: skip
        types = dict(c.execute(select(RiskTypes.c.RiskType_Id, RiskTypes.c.RiskTypeName)).all())
        md = c.execute(select(MatrixData).where(MatrixData.c.metric_id == debt_id)).mappings().one()
        cmd = CompanyMetricDetail.c
        detail = (
            c.execute(select(CompanyMetricDetail).where(cmd.metric_id == debt_id)).mappings().one()
        )
    assert (comp["CIK"], comp["NameAbbreviation"], comp["logo"]) == ("0001048286", "MAR", None)
    assert key["isActive"] and key["iconType"] == "warning"
    sources = json.loads(key["source"])
    assert sources[0]["title"] and key["link"] == next(s["url"] for s in sources if s["url"])
    # two distinct sources (0.85) + one is an SEC filing link (0.05)
    assert float(key["confidence"]) == 0.9
    by_cat = {r["Category"]: r for r in risks}
    assert types[by_cat["behavior"]["RiskType_Id"]] == "Emerging Risk Language"
    assert types[by_cat["risk"]["RiskType_Id"]] == "Liquidity & Leverage"  # "Balance Sheet"
    assert q["Priority"] == "high" and q["followingRisk"] == "Refinancing risk"
    risk_id = next(
        r["StrategicRiskId"] for r in risks if r["Title"] == "Negative equity with heavy debt"
    )
    assert ("KEY_INSIGHT", key["UniqueID"], "STRATEGIC_RISK", risk_id) in links
    assert ("KEY_INSIGHT", key["UniqueID"], "METRIC", debt_id) in links
    assert ("FOLLOWUP", q["FollowUpQuestionId"], "METRIC", debt_id) in links
    assert ("STRATEGIC_RISK", risk_id, "METRIC", debt_id) in links  # added by metricdetail
    assert (md["source_class"], md["fiscal_period"], float(md["confidence"])) == (
        "REPORTED",
        "Q2",
        1.0,
    )
    assert detail["UnderwritingRelevance"] and "Reported in XBRL" in detail["Methodology"]


def test_republish_is_idempotent_and_new_versions_retire_old_rows() -> None:
    engine, app, llm = _env()
    _publish(engine, app)
    before = {t.name: _count(app, t) for t in (KeyInsights, StrategicRisk, ItemLink, MatrixData)}
    assert "0 written" in _publish(engine, app)
    assert {
        t.name: _count(app, t) for t in (KeyInsights, StrategicRisk, ItemLink, MatrixData)
    } == before
    q_before = _count(app, FollowUpQuestions)
    # A new signal changes the strategic, key and follow-up inputs -> new versions.
    with engine.begin() as c:
        c.execute(insert(signal).values(
            signal_key="k2", company_id=1, rule_id="8k_5.02", category="Governance & Leadership",
            subcategory="Leadership Change", severity="Info", title="Director departure",
            description="d", event_date="2026-09-01", method="RULE", review_status="RULE_ONLY",
            detected_at=ti.NOW))  # fmt: skip
    ti._run(engine, llm)
    tm._run(engine, llm)
    _publish(engine, app)
    assert _count(app, KeyInsights) == 2 and _count(app, KeyInsights, KeyInsights.c.isActive) == 1
    assert _count(app, FollowUpQuestions) == q_before + 1
    assert _count(app, FollowUpQuestions, FollowUpQuestions.c.isActive) == 1
    # Links only connect active rows.
    with app.connect() as c:
        active_keys = set(
            c.execute(select(KeyInsights.c.UniqueID).where(KeyInsights.c.isActive)).scalars()
        )
        froms = set(c.execute(select(ItemLink.c.FromId)
                              .where(ItemLink.c.FromType == "KEY_INSIGHT")).scalars())  # fmt: skip
    assert froms <= active_keys


def test_app_rows_deleted_outside_are_recreated() -> None:
    engine, app, _ = _env()
    _publish(engine, app)
    with app.begin() as c:
        c.execute(KeyInsights.delete())
    _publish(engine, app)
    assert _count(app, KeyInsights) == 1


def test_question_flag_target_is_stable() -> None:
    """A user flag points at FollowUpQuestionId; republishing never reuses it for another text."""
    engine, app, _ = _env()
    _publish(engine, app)
    with app.connect() as c:
        qid, text = c.execute(select(FollowUpQuestions.c.FollowUpQuestionId,
                                     FollowUpQuestions.c.title)).one()  # fmt: skip
    with engine.begin() as c:  # the pipeline retires the question
        c.execute(update(followup_question).values(is_current=False))
    _publish(engine, app)
    with app.connect() as c:
        fq = FollowUpQuestions.c
        row = (
            c.execute(select(FollowUpQuestions).where(fq.FollowUpQuestionId == qid))
            .mappings()
            .one()
        )
    assert row["title"] == text and not row["isActive"]
