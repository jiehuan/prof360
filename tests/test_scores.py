"""Phase 9.4 scores stage and the news materiality gate for insights."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.package import build_package, load_insights_config
from uw_pipeline.jobs.run_scores import load_scores_config, run_scores_stage
from uw_pipeline.schema import (
    activity,
    company,
    filing_section,
    metadata,
    news_risk_score,
    section_chunk,
    source_document,
    transcript_flag,
    transcript_tone,
)

NOW = datetime(2026, 10, 13, tzinfo=UTC)
CHUNKS = ["Demand remains robust. We are confident in our full-year outlook.",
          "We received a subpoena from the SEC regarding our revenue recognition practices. "
          "We are cooperating fully."]  # fmt: skip


@pytest.fixture
def engine():
    e = create_engine("sqlite://")
    metadata.create_all(e)
    with e.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        act = dict(company_id=1, source_name="Reuters", url_hash="h", source_count=1,
                   first_seen_at=NOW, updated_at=NOW)  # fmt: skip
        c.execute(insert(activity), [
            act | {"activity_id": 1, "title": "Marriott sued over data breach",
                   "published_at": datetime(2026, 10, 1), "url": "u1", "url_hash": "h1",
                   "av_summary": "A class action seeks $100 million."},
            act | {"activity_id": 2, "title": "Marriott opens hotel in Lisbon",
                   "published_at": datetime(2026, 10, 2), "url": "u2", "url_hash": "h2",
                   "av_summary": "A new property."},
            act | {"activity_id": 3, "title": "Marriott CFO comments",
                   "published_at": datetime(2026, 10, 3), "url": "u3", "url_hash": "h3",
                   "av_summary": "Remarks at a conference."},
        ])  # fmt: skip
        c.execute(insert(source_document).values(
            doc_id=9, company_id=1, source_type="TRANSCRIPT", form_type="EARNINGS_CALL",
            doc_role="PRIMARY", accession_no="AV-MAR-2026Q2", doc_name="t", filed_at="2026-10-13",
            period_end="2026-06-30", url="u", content_hash="h"))  # fmt: skip
        text = "\n".join(CHUNKS)
        c.execute(insert(filing_section).values(section_id=1, doc_id=9, section_code="TRANSCRIPT",
                  start_offset=0, char_count=len(text), text=text, text_hash="th"))  # fmt: skip
        c.execute(insert(section_chunk), [
            {"section_id": 1, "chunk_index": i, "start_offset": 0, "end_offset": len(t), "text": t}
            for i, t in enumerate(CHUNKS)
        ])  # fmt: skip
    return e


def answer(model, user):
    if "id | date | headline" in user:
        return {"articles": [
            {"id": "A1", "litigation": 1.4, "regulatory": 0.1, "financial_distress": 0.2,
             "governance": 0.0, "operational": 0.3, "overall": 0.65,
             "rationale": "A class action seeks $100 million over the breach."},
            {"id": "A2", "litigation": 0, "regulatory": 0, "financial_distress": 0,
             "governance": 0, "operational": 0, "overall": 0.05, "rationale": "Routine opening."},
            {"id": "A3", "litigation": 0, "regulatory": 0, "financial_distress": 0,
             "governance": 0.2, "operational": 0, "overall": 0.2,
             "rationale": "The CFO said margins rose 12%."},  # invented number -> dropped
            {"id": "A99", "litigation": 0, "regulatory": 0, "financial_distress": 0,
             "governance": 0, "operational": 0, "overall": 0, "rationale": "x"},
        ]}  # fmt: skip
    return {"chunks": [
        {"chunk": "C0", "tone": 0.8, "confidence": 0.9, "hedging": 0.1, "uncertainty": 0.1,
         "forward_looking": 0.7, "liability_cues": 0.4,
         "flags": [{"category": "guidance",
                    "quote": "We are confident in our full-year outlook."}]},
        {"chunk": "C1", "tone": -2.0, "confidence": 0.5, "hedging": 0.3, "uncertainty": 0.6,
         "forward_looking": 0.0, "liability_cues": 0.9,
         "flags": [{"category": "regulatory",
                    "quote": "We received a subpoena from the SEC regarding our revenue "
                             "recognition practices."},
                   {"category": "accounting", "quote": "We restated three years of results."}]},
    ]}  # fmt: skip


def _run(engine, llm):
    res = run_scores_stage([CompanyConfig(ticker="MAR", cik=1048286)],
                           (llm, load_llm_config(CONFIG_DIR)), load_scores_config(CONFIG_DIR),
                           engine, run_id=5, now=lambda: NOW)  # fmt: skip
    assert res[0].ok, res
    return res[0].detail


def test_scores_are_clamped_grounded_and_cached(engine) -> None:
    llm = FakeLLM(answer)
    _run(engine, llm)
    with engine.connect() as c:
        news = {r.activity_id: r for r in c.execute(select(news_risk_score))}
        tone = c.execute(select(transcript_tone)).one()
        flags = c.execute(select(transcript_flag).order_by(transcript_flag.c.flag_index)).all()
    assert set(news) == {1, 2, 3}  # A99 does not exist
    assert news[3].rationale == ""  # "12%" is not in the article: rationale withheld
    assert news[1].litigation == 1.0 and news[1].run_id == 5  # clamped
    # length-weighted: chunk 1 is longer; its tone -2.0 is clamped to -1.0
    w0, w1 = len(CHUNKS[0]), len(CHUNKS[1])
    assert tone.overall_tone == round((0.8 * w0 - 1.0 * w1) / (w0 + w1), 2)
    assert (tone.n_chunks, tone.n_chunks_attempted) == (2, 2)
    # the invented restatement quote is dropped; the most liability-laden chunk comes first
    assert [(f.category, f.chunk_index) for f in flags] == [("regulatory", 1), ("guidance", 0)]
    calls = len(llm.calls)
    assert "news 0 scored/3 unchanged" in _run(engine, llm)
    assert len(llm.calls) == calls


def test_only_material_news_enters_the_insights_package(engine) -> None:
    _run(engine, FakeLLM(answer))
    icfg = load_insights_config(CONFIG_DIR)
    with engine.connect() as c:
        row = dict(c.execute(select(company)).mappings().one())
        metrics = load_registry(CONFIG_DIR).for_company(
            "MAR", load_industry_config(CONFIG_DIR).groups["HOTELS"]).metrics  # fmt: skip
        pkg = build_package(c, row, metrics, "Hotels", icfg, news_since=datetime(2026, 9, 1))
    assert [s.activity_id for s in pkg.of("NEWS")] == [1]  # overall 0.65; the opening is routine
