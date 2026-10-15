"""Phase 8 metricdetail stage: rule-based methodology and sources, LLM relevance and links."""

from sqlalchemy import create_engine, func, select

from tests import test_insights as ti
from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import load_insights_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.jobs.run_metric_detail import run_metric_detail_stage
from uw_pipeline.schema import (
    followup_question,
    followup_question_metric,
    insight,
    insight_metric,
    metadata,
    metric_detail,
    metric_insight,
    metric_source,
)


def answer(model, user):
    if "## Metrics (code | name" in user:
        return {"metrics": [
            {"metric_code": "total_debt", "uw_relevance": "Debt of $16.9B against negative equity.",
             "related_insight_ids": ["K1", "R1", "R9"], "related_question_ids": ["Q1"]},
            {"metric_code": "equity", "uw_relevance": "Equity fell 80% in a year.",  # invented
             "related_insight_ids": [], "related_question_ids": []},
            {"metric_code": "bogus", "uw_relevance": "x", "related_insight_ids": [],
             "related_question_ids": []},
        ]}  # fmt: skip
    return ti.answer(model, user)


def _run(engine, llm):
    res = run_metric_detail_stage(
        [CompanyConfig(ticker="MAR", cik=1048286)], load_registry(CONFIG_DIR),
        load_industry_config(CONFIG_DIR), (llm, load_llm_config(CONFIG_DIR)),
        load_insights_config(CONFIG_DIR), load_source_labels(CONFIG_DIR), engine,
        now=lambda: ti.NOW,
    )  # fmt: skip
    assert res[0].ok, res
    return res[0].detail


def _env():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    ti._seed(engine)
    return engine


def test_methodology_sources_relevance_and_links() -> None:
    engine = _env()
    llm = FakeLLM(answer)
    ti._run(engine, llm)
    detail = _run(engine, llm)
    assert "3 metrics" in detail and "relevance=1/3" in detail
    with engine.connect() as c:
        md = {r.metric_code: r for r in c.execute(select(metric_detail))}
        src = {(r.metric_code, r.period_type): r for r in c.execute(select(metric_source))}
        rel = c.execute(select(metric_insight)).mappings().all()
        key_id, risk_id = (
            c.execute(select(insight.c.insight_id).where(insight.c.title == t)).scalar_one()
            for t in ("Negative equity amid $16.9B debt", "Negative equity with heavy debt")
        )
        im = {tuple(r) for r in c.execute(select(insight_metric))}
        qid = c.execute(select(followup_question.c.question_id)).scalar_one()
        qm = {tuple(r) for r in c.execute(select(followup_question_metric))}
    assert set(md) == {"total_debt", "equity", "debt_to_equity"}
    assert "Formula: Total Debt / Stockholders' Equity." in md["debt_to_equity"].methodology
    assert "Shown as N/M when Stockholders' Equity ≤ 0." in md["debt_to_equity"].methodology
    assert md["debt_to_equity"].latest_value_text == "N/M"
    assert md["total_debt"].latest_period == "FY2026 Q2"
    assert "Reported in XBRL: 1" in md["total_debt"].methodology
    assert src[("total_debt", "Q")].title.startswith("Total Debt FY2026 Q2 — MARRIOTT")
    assert ("total_debt", "FY") not in src  # no fiscal-year value seeded
    # The invented number and the unknown code are dropped.
    assert [(r["metric_code"], r["is_current"]) for r in rel] == [("total_debt", True)]
    # K1 -> total_debt existed already (chosen with the insight); R1 is added by this stage;
    # R9 does not exist.
    assert (key_id, "total_debt", "INSIGHT") in im
    assert (risk_id, "total_debt", "METRIC_DETAIL") in im
    assert (key_id, "total_debt", "METRIC_DETAIL") not in im
    assert (qid, "total_debt", "QUESTION") in qm


def test_rerun_unchanged_makes_no_calls_and_keeps_links() -> None:
    engine = _env()
    llm = FakeLLM(answer)
    ti._run(engine, llm)
    _run(engine, llm)
    calls = len(llm.calls)
    assert "relevance=unchanged" in _run(engine, llm)
    assert len(llm.calls) == calls
    with engine.connect() as c:
        n = c.execute(
            select(func.count()).select_from(insight_metric)
            .where(insight_metric.c.link_source == "METRIC_DETAIL")
        ).scalar_one()  # fmt: skip
        assert n == 1


def test_rules_run_without_llm() -> None:
    engine = _env()
    res = run_metric_detail_stage(
        [CompanyConfig(ticker="MAR", cik=1048286)], load_registry(CONFIG_DIR),
        load_industry_config(CONFIG_DIR), None, load_insights_config(CONFIG_DIR),
        load_source_labels(CONFIG_DIR), engine, now=lambda: ti.NOW,
    )  # fmt: skip
    assert res[0].ok and "relevance=skipped" in res[0].detail
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(metric_detail)).scalar_one() == 3


def test_dq_rules_for_detail_modals() -> None:
    from sqlalchemy import update

    from uw_pipeline.quality.dq import load_dq_config, run_rules

    engine = _env()
    llm = FakeLLM(answer)
    ti._run(engine, llm)
    _run(engine, llm)
    reg = load_registry(CONFIG_DIR)
    metrics = reg.for_company("MAR", load_industry_config(CONFIG_DIR).groups["HOTELS"]).metrics
    cfg = load_dq_config(CONFIG_DIR)

    def findings():
        with engine.connect() as c:
            out = run_rules(c, 1, "MAR", metrics, {}, cfg, lambda: ti.NOW)
        return [f for f in out if f.rule_id in ("detail_coverage", "source_coverage",
                                                "link_integrity")]  # fmt: skip

    got = findings()
    # Only 1 of 3 metrics has a relevance (the others were dropped by the fake model's output);
    # the transcript-only behavioral insight has a source but no public link.
    assert sorted((f.rule_id, f.severity) for f in got) == [
        ("detail_coverage", "Warning"), ("source_coverage", "Info")]  # fmt: skip
    # Retiring the R insight a current key insight links to breaks the links.
    with engine.begin() as c:
        c.execute(update(insight).where(insight.c.title == "Negative equity with heavy debt")
                  .values(is_current=False))  # fmt: skip
    broken = [f.message for f in findings() if f.rule_id == "link_integrity"]
    assert any("current insight" in m for m in broken)
    assert any("current question" in m for m in broken)
