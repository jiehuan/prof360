"""Phase 5b insights on a synthetic company with a fake LLM (no API calls)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, insert, select, true

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import fmt_value, load_insights_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.jobs.run_insights import run_insights_stage
from uw_pipeline.schema import (
    company,
    filing_section,
    followup_question,
    followup_question_insight,
    followup_question_metric,
    followup_question_source,
    insight,
    insight_evidence,
    insight_link,
    insight_metric,
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


def _seed(engine, cid=1, cik="0001048286", ticker="MAR", name="MARRIOTT"):
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=cid, cik=cik, ticker=ticker, name=name.title(), legal_name=name,
            industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        mv = dict(company_id=cid, period_type="Q", fiscal_year=2026, fiscal_quarter=2,
                  period_end="2026-06-30", value_status="OK", derivation="REPORTED",
                  formula="f", source_tags="{}", source_accn="a", computed_at=NOW)  # fmt: skip
        c.execute(insert(metric_value), [
            mv | {"metric_code": "total_debt", "value": 16.915e9, "unit": "USD"},
            mv | {"metric_code": "equity", "value": -4.525e9, "unit": "USD"},
            mv | {"metric_code": "debt_to_equity", "value": None, "value_status": "NM", "unit": "ratio"},
        ])  # fmt: skip
        c.execute(insert(signal).values(
            signal_key=f"k{cid}", company_id=cid, rule_id="negative_equity", category="Risk & Legal",
            subcategory="Balance Sheet", severity="Warning",
            title="Stockholders' equity is negative; debt-to-equity is not meaningful",
            description="d", event_date="2026-06-30", snippet=None, method="RULE",
            review_status="RULE_ONLY", detected_at=NOW))  # fmt: skip
        docs = [(1, "10-K", "SEC_FILING", "acc-k", "2026-02-10", None, "10K-1A", RISK),
                (2, "EARNINGS_CALL", "TRANSCRIPT", "AV-MAR-2026Q1", "2026-05-06", "2026-03-31", "TRANSCRIPT", CALL_1),
                (3, "EARNINGS_CALL", "TRANSCRIPT", "AV-MAR-2026Q2", "2026-08-03", "2026-06-30", "TRANSCRIPT", CALL_2)]  # fmt: skip
        for doc_id, form, stype, accn, filed, pend, code, text in docs:
            doc_id += 10 * (cid - 1)
            c.execute(insert(source_document).values(
                doc_id=doc_id, company_id=cid, source_type=stype, form_type=form,
                doc_role="PRIMARY", accession_no=f"{accn}-{cid}", doc_name="d", filed_at=filed,
                period_end=pend, url="u", content_hash="h"))  # fmt: skip
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
    if "## Rule-based signals" in user and "\nK1 | " in user:  # key takeaways (K items exist)
        return {"takeaways": [
            {"topic": "Leverage and refinancing",
             "text": "Debt of $16.9B against negative equity raises refinancing and disclosure risk.",
             "evidence": [_ev("K1", "Negative equity amid $16.9B debt")]},
            {"topic": "Emerging macro caution",
             "text": "The CEO's turn to a more challenging US macro view may precede guidance cuts.",
             "evidence": [_ev("B1", "Macro caution enters CEO remarks")]},
            {"topic": "Litigation", "text": "A 40% revenue drop raises securities claim risk.",
             "evidence": [_ev("R1", "Negative equity with heavy debt")]},  # invented number
        ]}  # fmt: skip
    if "## Rule-based signals" in user:  # key insights
        return {"insights": [
            {"type": "Warning", "title": "Negative equity amid $16.9B debt",
             "summary": "Equity is negative and debt is $16.9B.", "uw_relevance": "Leverage.",
             "supporting_evidence": "Total debt $16.9B.", "related_metric_codes": ["total_debt", "bogus"],
             # M3 = total_debt (metric rows are sorted by code)
             "evidence": [_ev("M3", "$16.91B"), _ev("R1", "Negative equity with heavy debt")]},
            # Grounded, but its summary has 187 characters: longer than the card shows (D-065).
            {"type": "Warning", "title": "Refinancing exposure",
             "summary": "Debt of $16.9B sits against negative equity, which leaves little room if "
                        "earnings weaken, lenders tighten covenants or refinancing markets close, "
                        "and raises the risk of disclosure claims.",
             "uw_relevance": "Leverage.", "supporting_evidence": "Total debt $16.9B.",
             "related_metric_codes": ["total_debt"],
             "evidence": [_ev("M3", "$16.91B")]},
        ]}  # fmt: skip
    if "## Insights from earlier steps" in user:  # follow-up questions
        return {"questions": [
            {"category": "Financial", "question": "How will you refinance the $16.9B of debt?",
             "summary": "Total debt is $16.9B and equity is negative.",
             "following_risk": "Refinancing risk", "uw_relevance": "Refinancing risk.",
             "system_priority": "high", "related_insight_ids": ["K1", "R1"],
             "related_metric_ids": ["M3", "M99"]},
            {"category": "Financial", "question": "Why did revenue fall 37%?", "summary": "x",
             "following_risk": "x", "uw_relevance": "Made-up number.", "system_priority": "low",
             "related_insight_ids": [], "related_metric_ids": []},
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
        load_insights_config(CONFIG_DIR), load_source_labels(CONFIG_DIR), engine, now=lambda: NOW,
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
        ("TAKEAWAY", "Leverage and refinancing"),  # the invented-number takeaway is dropped
        ("TAKEAWAY", "Emerging macro caution"),
    }
    assert "key_takeaways=2/3" in detail
    # Two key insights generated: the one whose summary is over 180 characters is dropped.
    assert "key_insights=1/2" in detail
    lead = rows[("TAKEAWAY", "Leverage and refinancing")]
    assert lead.ordinal == 1 and lead.summary.startswith("Debt of $16.9B")
    # Its evidence resolves through K1 to K1's own sources (kept for audits, not shown).
    assert {e["source_kind"] for e in ev if e["insight_id"] == lead.insight_id} == {
        "METRIC",
        "DOCUMENT",
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
    assert q[0]["following_risk"] == "Refinancing risk" and q[0]["summary"]
    assert links == 2


def test_sources_links_and_evidence_text(env) -> None:
    _run(env, FakeLLM(answer))
    with env.connect() as c:
        rows = {r.title: r for r in c.execute(select(insight))}
        key = rows["Negative equity amid $16.9B debt"]
        risk = rows["Negative equity with heavy debt"]
        beh = rows["Macro caution enters CEO remarks"]
        ev = c.execute(select(insight_evidence).where(
            insight_evidence.c.insight_id == key.insight_id)).mappings().all()  # fmt: skip
        link = c.execute(select(insight_link)).all()
        metrics = c.execute(select(insight_metric.c.metric_code).where(
            insight_metric.c.insight_id == key.insight_id)).scalars().all()  # fmt: skip
        qid = c.execute(select(followup_question.c.question_id)).scalar_one()
        qm = c.execute(select(followup_question_metric.c.metric_code)).scalars().all()
        qs = c.execute(select(followup_question_source).order_by(
            followup_question_source.c.ordinal)).mappings().all()  # fmt: skip
    # K1 cited R1: the link is kept, and the evidence resolves to the 10-K with a readable title.
    assert [x for x in link if x[0] == key.insight_id] == [(key.insight_id, risk.insight_id)]
    doc_ev = next(e for e in ev if e["source_kind"] == "DOCUMENT")
    assert doc_ev["source_title"] == "MARRIOTT 10-K filed 2026-02-10 · Item 1A. Risk Factors"
    assert doc_ev["source_url"] == "u" and doc_ev["source_date"] == "2026-02-10"
    metric_ev = next(e for e in ev if e["source_kind"] == "METRIC")
    assert metric_ev["source_title"].startswith("Total Debt FY2026 Q2")
    assert "\u201cWe have significant indebtedness\u201d" in key.supporting_evidence_text
    # The metric list includes the cited metric row; unknown codes are dropped.
    assert metrics == ["total_debt"]
    assert key.updated_at == "2026-02-10"
    # Transcripts have no public link; their date is unknown, so updated_at stays empty.
    assert beh.updated_at is None
    # Question: metric ids map to codes (M99 does not exist); sources come from K1/R1 evidence
    # and the filing behind total_debt.
    assert qm == ["total_debt"]
    assert [(s["question_id"], s["source_kind"]) for s in qs] == [
        (qid, "DOCUMENT"),
        (qid, "METRIC"),
    ]
    assert qs[0]["title"] == doc_ev["source_title"]
    # A value whose filing is not registered still links to the EDGAR filing index.
    assert qs[1]["url"] == "https://www.sec.gov/Archives/edgar/data/1048286/a/a-index.html"


def test_rerun_with_same_inputs_makes_no_calls(env) -> None:
    llm = FakeLLM(answer)
    _run(env, llm)
    calls = len(llm.calls)
    detail = _run(env, llm)
    assert len(llm.calls) == calls
    assert detail.count("unchanged") == 5
    with env.connect() as c:
        assert c.execute(select(func.count()).select_from(insight)).scalar_one() == 6


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
            select(insight.c.kind, func.count()).where(insight.c.is_current == true())
            .group_by(insight.c.kind)).all()  # fmt: skip
        total = c.execute(select(func.count()).select_from(insight)).scalar_one()
    assert dict(current) == {"BEHAVIORAL": 1, "STRATEGIC_RISK": 2, "KEY": 1, "TAKEAWAY": 2}
    # behavioral input unchanged; strategic (2 rows), key (1) and takeaways (2) re-generated
    assert total == 6 + 5


def test_fmt_value() -> None:
    assert fmt_value(16.915e9, "USD") == "$16.91B"
    assert fmt_value(0.0485, "fraction") == "4.9%"
    assert fmt_value(5.5611, "ratio") == "5.56x"
    assert fmt_value(None, "ratio", "NM") == "N/M"


def test_number_grounding_does_not_match_inside_decimals() -> None:
    from uw_pipeline.enrich.grounding import ungrounded_numbers

    assert ungrounded_numbers("an impairment of $27 million", ["charges of 27.5 million"], []) == [
        "$27 million"
    ]
    assert (
        ungrounded_numbers("an impairment of $27.5 million", ["charges of 27.5 million"], []) == []
    )


def test_numbers_in_metric_names_are_input() -> None:
    """'$100' in "value of $100 invested" (TSR, D-042) is part of the input, not made up."""
    from uw_pipeline.enrich.insights import Cited, GroundingStats, numbers_ok
    from uw_pipeline.enrich.package import Package, Source

    pkg = Package("Acme", "Retail", False)
    pay = Source(
        id="M1",
        kind="METRIC",
        label="CEO Total Compensation FY2025",
        text="CEO Total Compensation | FY2025 | $26.0M | REPORTED",
        values=(26e6,),
    )
    pkg.add(pay)
    pkg.add(Source(id="M2", kind="METRIC", label="Company TSR (value of $100 invested) FY2025",
                   text="Company TSR (value of $100 invested) | FY2025 | $120 | REPORTED",
                   values=(120.0,)))  # fmt: skip
    text = ["CEO pay rose to $26.0M while $100 invested fell to $120."]
    assert numbers_ok(text, [Cited(pay, "$26.0M")], pkg, GroundingStats(), "t")
    assert not numbers_ok(["CEO pay rose to $31M."], [Cited(pay, "$26.0M")], pkg,
                          GroundingStats(), "t")  # fmt: skip


def test_companies_run_in_parallel_on_a_sqlite_file(tmp_path) -> None:
    """D-052: the stage commits its writes before each model call, so two companies' calls
    overlap on a SQLite file instead of the second waiting for the first one's transaction."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from uw_pipeline.clients.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'i.db'}")
    metadata.create_all(engine)
    _seed(engine)
    _seed(engine, cid=2, cik="0001585689", ticker="HLT", name="HILTON")
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def slow(model, user):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.3)
        with lock:
            state["now"] -= 1
        return answer(model, user)

    llm = FakeLLM(slow)
    args = (load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR),
            (llm, load_llm_config(CONFIG_DIR)), load_insights_config(CONFIG_DIR),
            load_source_labels(CONFIG_DIR), engine)  # fmt: skip
    companies = [CompanyConfig(ticker="MAR", cik=1048286), CompanyConfig(ticker="HLT", cik=1585689)]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: run_insights_stage([c], *args, now=lambda: NOW)[0],
                                companies))  # fmt: skip
    elapsed = time.monotonic() - started
    assert all(r.ok for r in results), results
    assert state["peak"] == 2 and len(llm.calls) == 10  # five calls each, two at a time
    # 1.5 s of calls per company: together ~1.6 s; one after the other (the second waiting on
    # the first one's transaction) ~2.7 s.
    assert elapsed < 2.2, elapsed
    with engine.connect() as c:
        per_company = dict(c.execute(select(insight.c.company_id, func.count())
                                     .group_by(insight.c.company_id)).all())  # fmt: skip
    assert per_company == {1: 6, 2: 6}
