"""Phase 17g (D-059): an industry group chosen by the LLM for a company whose SIC code maps to
none: the checks, both models, the review queue and the company stage keeping the choice.

The business section is a recorded excerpt of SSR Mining's 10-K filed 2026-02-17 (SIC 6795,
mineral royalty traders, in no group); model answers come from tests/fake_llm.py.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.industry_choice import catalog, checked, schema, usable_choice
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.schemas import IndustryChoice
from uw_pipeline.jobs.run_industry import run_industry_stage
from uw_pipeline.jobs.run_review import decide, pending
from uw_pipeline.schema import (
    company,
    filing_section,
    industry_choice,
    metadata,
    source_document,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)
ITEM1 = (Path(__file__).parent / "fixtures" / "sec" / "industry"
         / "ssrm_10k_item1_excerpt.txt").read_text()  # fmt: skip
QUOTE = ("SSR Mining Inc. and its subsidiaries (collectively, “SSR Mining,” or "
         "“Company”) is a precious metals mining company")  # fmt: skip
INDUSTRY = load_industry_config(CONFIG_DIR)
REGISTRY = load_registry(CONFIG_DIR)
SSRM = CompanyConfig(ticker="SSRM", cik=921638)


def answer(group: str, quote: str = QUOTE) -> dict:
    return {"group": group, "quote": quote, "reason": "A gold and silver mining company."}


def test_only_configured_groups_and_grounded_quotes() -> None:
    assert schema(INDUSTRY)["properties"]["group"]["enum"][0] == "HOTELS"
    assert "UNCLASSIFIED" not in schema(INDUSTRY)["properties"]["group"]["enum"]
    assert "MINING: Mining & Quarrying (operating company). Adds:" in catalog(INDUSTRY, REGISTRY)
    ok = IndustryChoice.model_validate(answer("MINING"))
    assert checked(ok, INDUSTRY, ITEM1) == "MINING"
    assert checked(IndustryChoice.model_validate(answer("GOLD_MINERS")), INDUSTRY, ITEM1) is None
    made_up = answer("MINING", "SSR Mining is the largest gold producer in Nevada")
    assert checked(IndustryChoice.model_validate(made_up), INDUSTRY, ITEM1) is None


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0000921638", ticker="SSRM", name="SSR Mining",
            legal_name="SSR MINING INC.", sic_code="6795",
            sic_description="Mineral Royalty Traders", industry_group="UNCLASSIFIED",
            is_financial=False, is_active=True, fiscal_year_end="1231", source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        c.execute(insert(source_document).values(
            doc_id=1, company_id=1, source_type="SEC_FILING", form_type="10-K",
            doc_role="PRIMARY", accession_no="0000921638-26-000010", doc_name="d", url="u",
            filed_at="2026-02-17"))  # fmt: skip
        c.execute(insert(filing_section).values(
            doc_id=1, section_code="10K-1", start_offset=0, char_count=len(ITEM1), text=ITEM1,
            text_hash="h"))  # fmt: skip
    return engine


def _run(engine, llm, companies=(SSRM,)):
    recomputed: list[str] = []

    def recompute(cfgs):
        recomputed.extend(c.ticker for c in cfgs)
        return []

    res = run_industry_stage(list(companies), (llm, load_llm_config(CONFIG_DIR)), REGISTRY,
                             INDUSTRY, engine, recompute, now=lambda: NOW)  # fmt: skip
    assert all(r.ok for r in res), res
    with engine.connect() as c:
        group = c.execute(select(company.c.industry_group)).scalar()
        choice = c.execute(select(industry_choice)).mappings().first()
    return res, group, choice, recomputed


def test_both_models_agree_the_company_gets_the_group_and_is_recomputed() -> None:
    engine = _db()
    llm = FakeLLM(lambda model, user: answer("MINING"))
    res, group, choice, recomputed = _run(engine, llm)
    assert (group, choice["status"], recomputed) == ("MINING", "AGREED", ["SSRM"])
    assert res[0].detail == "MINING (agreed)"
    assert len(llm.calls) == 2
    with engine.connect() as c:
        assert pending(c) == []
    # The next run reuses the cached answers: no calls, no recompute (nothing changed).
    _, group, _, recomputed = _run(engine, llm)
    assert (group, len(llm.calls), recomputed) == ("MINING", 2, [])
    # A company in a group by its SIC code or companies.yaml is not read.
    in_group = CompanyConfig(ticker="SSRM", cik=921638, industry="METALS", industry_reason="x")
    res, *_ = _run(engine, llm, [in_group])
    assert res[0].detail.startswith("in MINING (company)") and len(llm.calls) == 2


def test_disagreement_uses_the_tie_breaker_and_waits_for_review() -> None:
    engine = _db()
    cfg = load_llm_config(CONFIG_DIR)

    def two_minds(model, user):
        return answer("MINING" if model == cfg.extractor.model else "METALS")

    _, group, choice, _ = _run(engine, FakeLLM(two_minds))
    assert (group, choice["status"]) == ("METALS", "DISPUTED")
    assert (choice["extractor_group"], choice["tiebreaker_group"]) == ("MINING", "METALS")
    with engine.begin() as c:
        [item] = pending(c)
        assert item.item_type == "INDUSTRY" and "METALS (models: MINING / METALS)" in item.title
        decide(c, item.short, "REJECTED", "A Reviewer", "a gold miner", NOW)
        assert usable_choice(c, 1) is None
    # Rejected: the company goes back to the default group.
    _, group, _, recomputed = _run(engine, FakeLLM(two_minds))
    assert (group, recomputed) == ("UNCLASSIFIED", ["SSRM"])


@pytest.mark.usefixtures("sec_routes")
def test_company_stage_keeps_a_stored_choice(make_sec_client) -> None:
    """The company stage classifies by SIC every run; a usable choice survives it."""
    from uw_pipeline.jobs.run_company import run_company_stage

    engine = _db()
    with engine.begin() as c:
        c.execute(insert(industry_choice).values(
            company_id=1, group_code="MINING", status="AGREED", quote=QUOTE, reason="r",
            doc_id=1, model="m", prompt_version="v", input_hash="h", updated_at=NOW))  # fmt: skip
    # Recorded submissions exist for the MVP companies only: reuse Marriott's under SSRM's CIK.
    mar = CompanyConfig(ticker="MAR", cik=1048286)
    with engine.begin() as c:
        c.execute(company.update().values(cik="0001048286", ticker="MAR"))
    [res] = run_company_stage([mar], make_sec_client(), INDUSTRY, engine)
    assert res.ok and res.detail == "HOTELS"  # SIC 7011 gives a group: the SIC wins
    with engine.begin() as c:
        c.execute(company.update().values(sic_code="6795"))
    hotel_free = INDUSTRY.model_copy(update={"sic": {}, "sic_ranges": []})
    [res] = run_company_stage([mar], make_sec_client(), hotel_free, engine)
    assert res.ok and res.detail == "MINING"  # no group by SIC: the stored choice
