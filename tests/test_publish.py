"""Phase 9 publish stage: pipeline outputs -> application tables (stable ids, versions, links)."""

import json

from sqlalchemy import create_engine, delete, event, func, insert, select, update

from tests import test_insights as ti
from tests import test_metric_detail as tm
from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.jobs.run_publish import load_publish_config, run_publish_stage
from uw_pipeline.schema import (
    followup_question,
    signal,
    source_document,
    transcript_flag,
    transcript_tone,
)
from uw_pipeline.schema_app import (
    Company,
    CompanyMetricDetail,
    FollowUpQuestions,
    ItemLink,
    KeyInsights,
    KeyTakeaways,
    MatrixData,
    RiskTypes,
    StrategicRisk,
    TranscriptRiskFlag,
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
        debt_sub = c.execute(select(metrics.c.subtitle)
                             .where(metrics.c.metric_id == debt_id)).scalar_one()  # fmt: skip
        types = dict(c.execute(select(RiskTypes.c.RiskType_Id, RiskTypes.c.RiskTypeName)).all())
        md = c.execute(select(MatrixData).where(MatrixData.c.metric_id == debt_id)).mappings().one()
        cmd = CompanyMetricDetail.c
        detail = (
            c.execute(select(CompanyMetricDetail).where(cmd.metric_id == debt_id)).mappings().one()
        )
    assert (comp["CIK"], comp["NameAbbreviation"]) == ("0001048286", "MAR")
    assert comp["logo"] == PCFG.logos["MAR"] and comp["logo"].startswith("data:image/")  # D-067
    assert debt_sub == "Borrowings due now and in later years"  # an explanation, not the unit
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
    # Data points carry the numbers as well as the text (2026-10-27): no parsing on the front end.
    pts = json.loads(detail["DataPoints"])
    q2 = next(p for p in pts if p["label"] == "FY2026 Q2")
    assert q2["value"] == md["value"] and q2["unit"] == "USD" and q2["status"] == "OK"
    assert (q2["fiscalYear"], q2["fiscalPeriod"], q2["source"]) == (2026, "Q2", "Reported in XBRL")
    assert q2["text"].startswith("$") and q2["periodEnd"] == str(md["period_end"])
    # Key takeaways: bullets in order, no links, nothing leaked into StrategicRisk.
    with app.connect() as c:
        tk = c.execute(select(KeyTakeaways).order_by(KeyTakeaways.c.Ordinal)).mappings().all()
    assert [t["Ordinal"] for t in tk] == [1, 2] and all(t["isActive"] for t in tk)
    assert tk[0]["Takeaway"].startswith("Debt of $16.9B")
    assert len(risks) == 3 and not any(t == "TAKEAWAY" for t, *_ in links)


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
    assert (
        _count(app, KeyTakeaways) == 4 and _count(app, KeyTakeaways, KeyTakeaways.c.isActive) == 2
    )
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


def test_transcript_flags_are_rewritten_only_when_they_change() -> None:
    engine, app, _ = _env()
    with engine.begin() as c:
        c.execute(insert(source_document).values(
            doc_id=900, company_id=1, source_type="TRANSCRIPT", form_type="EARNINGS_CALL",
            doc_role="PRIMARY", accession_no="AV-MAR-2026Q2", doc_name="t", filed_at="2026-08-01",
            url="u", content_hash="h"))  # fmt: skip
        c.execute(insert(transcript_tone).values(
            doc_id=900, company_id=1, overall_tone=0.1, n_chunks=2, n_chunks_attempted=2, model="m",
            prompt_version="v1", input_hash="i", run_id=5, created_at=ti.NOW))  # fmt: skip
        c.execute(insert(transcript_flag), [
            {"doc_id": 900, "flag_index": i, "category": "litigation", "flag_text": f"flag {i}",
             "chunk_index": 0} for i in range(2)])  # fmt: skip
    writes: list[str] = []

    @event.listens_for(app, "before_cursor_execute")
    def _record(conn, cursor, statement, *args) -> None:
        if "TranscriptRiskFlag" in statement and not statement.startswith("SELECT"):
            writes.append(statement)

    def flags():
        with app.connect() as c:
            return c.execute(select(TranscriptRiskFlag.c.runId, TranscriptRiskFlag.c.FlagIndex,
                                    TranscriptRiskFlag.c.FlagText)).all()  # fmt: skip

    _publish(engine, app)
    assert sorted(flags()) == [(5, 0, "flag 0"), (5, 1, "flag 1")]
    writes.clear()
    _publish(engine, app)
    assert writes == []  # unchanged: nothing deleted or rewritten
    with engine.begin() as c:  # rescored in run 6 with one flag
        c.execute(update(transcript_tone).values(run_id=6))
        c.execute(delete(transcript_flag).where(transcript_flag.c.flag_index == 1))
    _publish(engine, app)
    assert flags() == [(6, 0, "flag 0")]
    with engine.begin() as c:  # transcript no longer scored
        c.execute(delete(transcript_flag))
        c.execute(delete(transcript_tone))
    _publish(engine, app)
    assert flags() == []
