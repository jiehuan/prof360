"""Phase 5b insights on a synthetic company with a fake LLM (no API calls)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import fmt_value, load_insights_config
from uw_pipeline.jobs.run_insights import run_insights_stage
from uw_pipeline.schema import (
    company,
    filing_section,
    followup_question,
    followup_question_insight,
    insight,
    insight_evidence,
    metadata,
    metric_value,
    signal,
    source_document,
)

NOW = datetime(2026, 10, 13, tzinfo=UTC)
RISK = ("We have significant indebtedness. Our total debt could limit our flexibility.\n"
        "A cyberattack on our reservation system could disrupt bookings.")  # fmt: skip
CALL_1 = "Jane CEO (Chief Executive Officer): Demand remains robust across all regions."
CALL_2 = (
    "Jane CEO (Chief Executive Officer): We see a more challenging macro environment in the US."
)


def _seed(engine):
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        mv = dict(company_id=1, period_type="Q", fiscal_year=2026, fiscal_quarter=2,
                  period_end="2026-06-30", value_status="OK", derivation="REPORTED",
                  formula="f", source_tags="{}", source_accn="a", computed_at=NOW)  # fmt: skip
        c.execute(insert(metric_value), [
            mv | {"metric_code": "total_debt", "value": 16.915e9, "unit": "USD"},
            mv | {"metric_code": "equity", "value": -4.525e9, "unit": "USD"},
            mv | {"metric_code": "debt_to_equity", "value": None, "value_status": "NM", "unit": "ratio"},
        ])  # fmt: skip
        c.execute(insert(signal).values(
            signal_key="k1", company_id=1, rule_id="negative_equity", category="Risk & Legal",
            subcategory="Balance Sheet", severity="Warning",
            title="Stockholders' equity is negative; debt-to-equity is not meaningful",
            description="d", event_date="2026-06-30", snippet=None, method="RULE",
            review_status="RULE_ONLY", detected_at=NOW))  # fmt: skip
        docs = [(1, "10-K", "SEC_FILING", "acc-k", "2026-02-10", None, "10K-1A", RISK),
                (2, "EARNINGS_CALL", "TRANSCRIPT", "AV-MAR-2026Q1", "2026-05-06", "2026-03-31", "TRANSCRIPT", CALL_1),
                (3, "EARNINGS_CALL", "TRANSCRIPT", "AV-MAR-2026Q2", "2026-08-03", "2026-06-30", "TRANSCRIPT", CALL_2)]  # fmt: skip
        for doc_id, form, stype, accn, filed, pend, code, text in docs:
            c.execute(insert(source_document).values(
                doc_id=doc_id, company_id=1, source_type=stype, form_type=form, doc_role="PRIMARY",
                accession_no=accn, doc_name="d", filed_at=filed, period_end=pend, url="u",
                content_hash="h"))  # fmt: skip
            c.execute(insert(filing_section).values(
                doc_id=doc_id, section_code=code, start_offset=0, char_count=len(text),
                text=text, text_hash="h"))  # fmt: skip


def _ev(sid, quote):
    return {"source_id": sid, "quote": quote}


def answer(model, user):
    if "## Filing excerpts" in user:  # strategic & risk
        return {"insights": [
            {"category": "Risk & Legal", "subcategory": "Balance Sheet", "title": "Negative equity with heavy debt",
             "description": "Total debt was $16.9B while equity was -$4.5B.", "uw_relevance": "Leverage stress.",
             "event_date": None, "evidence": [_ev("E1", "We have significant indebtedness")]},
            {"category": "Risk & Legal", "subcategory": "Cyber", "title": "Invented breach",
             "description": "A breach exposed 40 million records.", "uw_relevance": "x",
             "event_date": None, "evidence": [_ev("E1", "A cyberattack on our reservation system")]},
            {"category": "Event Signals", "subcategory": "M&A", "title": "Unsupported quote",
             "description": "An acquisition was announced.", "uw_relevance": "x",
             "event_date": None, "evidence": [_ev("E1", "We acquired a hotel chain")]},
        ], "no_signal": [{"category": "Governance & Leadership", "rationale": "No leadership changes."}]}  # fmt: skip
    if "## Earnings call transcripts" in user:  # behavioral
        return {"insights": [
            {"subcategory": "Emerging Risk Language", "title": "Macro caution enters CEO remarks",
             "description": "The CEO moved from robust demand to a more challenging US macro.",
             "uw_relevance": "Softer outlook may precede guidance cuts.",
             "evidence": [_ev("T1", "Demand remains robust across all regions"),
                          _ev("T2", "more challenging macro environment in the US")]}]}  # fmt: skip
    if "## Rule-based signals" in user:  # key insights
        return {"insights": [
            {"type": "Warning", "title": "Negative equity amid $16.9B debt",
             "summary": "Equity is negative and debt is $16.9B.", "uw_relevance": "Leverage.",
             "supporting_evidence": "Total debt $16.9B.", "related_metric_codes": ["total_debt", "bogus"],
             # M3 = total_debt (metric rows are sorted by code)
             "evidence": [_ev("M3", "$16.91B"), _ev("R1", "Negative equity with heavy debt")]},
        ]}  # fmt: skip
    if "## Insights from earlier steps" in user:  # follow-up questions
        return {"questions": [
            {"category": "Financial", "question": "How will you refinance the $16.9B of debt?",
             "rationale": "Refinancing risk.", "system_priority": "high", "related_insight_ids": ["K1", "R1"]},
            {"category": "Financial", "question": "Why did revenue fall 37%?",
             "rationale": "Made-up number.", "system_priority": "low", "related_insight_ids": []},
        ]}  # fmt: skip
    raise AssertionError(f"unexpected prompt: {user[:200]}")


@pytest.fixture
def env():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    _seed(engine)
    return engine


def _run(engine, llm):
    res = run_insights_stage(
        [CompanyConfig(ticker="MAR", cik=1048286)], load_registry(CONFIG_DIR),
        load_industry_config(CONFIG_DIR), (llm, load_llm_config(CONFIG_DIR)),
        load_insights_config(CONFIG_DIR), engine, now=lambda: NOW,
    )  # fmt: skip
    assert res[0].ok, res
    return res[0].detail


def test_grounded_insights_stored_with_lineage(env) -> None:
    detail = _run(env, FakeLLM(answer))
    assert "strategic_risk=1/3" in detail  # invented number and unsupported quote dropped
    with env.connect() as c:
        rows = {(r.kind, r.title): r for r in c.execute(select(insight))}
        ev = c.execute(select(insight_evidence)).mappings().all()
        q = c.execute(select(followup_question)).mappings().all()
        links = c.execute(select(func.count()).select_from(followup_question_insight)).scalar_one()
    assert set(rows) == {
        ("BEHAVIORAL", "Macro caution enters CEO remarks"),
        ("STRATEGIC_RISK", "Negative equity with heavy debt"),
        ("STRATEGIC_RISK", "No material signal identified"),
        ("KEY", "Negative equity amid $16.9B debt"),
    }
    key_id = rows[("KEY", "Negative equity amid $16.9B debt")].insight_id
    key_ev = [e for e in ev if e["insight_id"] == key_id]
    # The metric citation is stored as such; the R1 citation resolves to the 10-K excerpt.
    assert {(e["source_kind"], e["metric_code"], e["doc_id"]) for e in key_ev} == {
        ("METRIC", "total_debt", None),
        ("DOCUMENT", None, 1),
    }
    beh_id = rows[("BEHAVIORAL", "Macro caution enters CEO remarks")].insight_id
    assert sorted(e["doc_id"] for e in ev if e["insight_id"] == beh_id) == [2, 3]
    assert [x["question"] for x in q] == ["How will you refinance the $16.9B of debt?"]
    assert links == 2


def test_rerun_with_same_inputs_makes_no_calls(env) -> None:
    llm = FakeLLM(answer)
    _run(env, llm)
    calls = len(llm.calls)
    detail = _run(env, llm)
    assert len(llm.calls) == calls
    assert detail.count("unchanged") == 4
    with env.connect() as c:
        assert c.execute(select(func.count()).select_from(insight)).scalar_one() == 4


def test_changed_input_creates_new_current_version(env) -> None:
    llm = FakeLLM(answer)
    _run(env, llm)
    with env.begin() as c:
        c.execute(insert(signal).values(
            signal_key="k2", company_id=1, rule_id="8k_5.02", category="Governance & Leadership",
            subcategory="Leadership Change", severity="Info", title="Director departure",
            description="d", event_date="2026-09-01", method="RULE", review_status="RULE_ONLY",
            detected_at=NOW))  # fmt: skip
    _run(env, llm)
    with env.connect() as c:
        current = c.execute(
            select(insight.c.kind, func.count()).where(insight.c.is_current.is_(True))
            .group_by(insight.c.kind)).all()  # fmt: skip
        total = c.execute(select(func.count()).select_from(insight)).scalar_one()
    assert dict(current) == {"BEHAVIORAL": 1, "STRATEGIC_RISK": 2, "KEY": 1}
    assert total == 4 + 3  # behavioral input unchanged; strategic (2 rows) and key re-generated


def test_fmt_value() -> None:
    assert fmt_value(16.915e9, "USD") == "$16.91B"
    assert fmt_value(0.0485, "fraction") == "4.9%"
    assert fmt_value(5.5611, "ratio") == "5.56x"
    assert fmt_value(None, "ratio", "NM") == "N/M"
