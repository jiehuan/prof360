from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.schemas import ExtractedValue
from uw_pipeline.enrich.text_metrics import (
    Candidate,
    candidates,
    dual_checked,
    load_text_config,
    number_in_quote,
    parse_number,
    period_keys,
    select_chunks,
)
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.normalize.periods import FiscalCalendar
from uw_pipeline.schema import company, metadata, metric_value, source_document

REG = load_registry(CONFIG_DIR)
IND = load_industry_config(CONFIG_DIR)
DEC = FiscalCalendar("1231")
NOW = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "value"),
    [("1,234.5", 1234.5), ("(42)", -42.0), ("$128.80", 128.8), ("69.3%", 69.3),
     ("\u2212 7", -7.0), ("n/a", None), ("12.3x", None)],
)  # fmt: skip
def test_parse_number(text, value) -> None:
    assert parse_number(text) == value


def test_number_must_appear_in_quote() -> None:
    row = "Worldwide (2) $ 138.74 4.1 % 71.6 % 0.8 % pts. $ 193.66"
    assert number_in_quote("138.74", row) and number_in_quote("71.6", row)
    assert not number_in_quote("138.47", row)
    assert number_in_quote("7,071", "Total revenues 7,071 6,744")
    assert number_in_quote("7071", "Total revenues 7,071 6,744")


def test_each_metric_gets_its_chunks() -> None:
    chunks = ["CET1 " * 50] * 5 + ["The net yield on these assets was 2.40%."] + ["filler"]
    picked = select_chunks(chunks, [["CET1"], ["net yield on"]], max_chars=600)
    assert 5 in picked  # the rare metric isn't crowded out by the frequent one
    assert select_chunks(["intro", "nothing"], [["CET1"]], 1000, keep_first=True) == [0]


def test_period_keys() -> None:
    assert period_keys("quarter", "2026-06-30", DEC, "2026-04-01") == [("Q", 2026, 2)]
    assert period_keys("point_in_time", "2025-12-31", DEC) == [("Q", 2025, 4), ("FY", 2025, 0)]
    assert period_keys("year", "2025-12-31", DEC, "2025-01-01") == [("FY", 2025, 0)]
    assert period_keys("year", "2025-06-30", DEC, "2024-07-01") == []
    assert period_keys("quarter", "June 2026", DEC, "2026-04-01") == []
    # SKYD: a merger stub (Aug 8 - Sep 30) in a "quarter" column is not a quarter.
    assert period_keys("quarter", "2025-09-30", DEC, "2025-08-08") == []
    assert period_keys("quarter", "2025-09-30", DEC, None) == []


QUARTER_START = {"2026-06-30": "2026-04-01", "2026-09-30": "2026-07-01", "2025-09-30": "2025-07-01"}


def ev(code, value, unit, period, end, evidence, scale="units"):
    start = QUARTER_START.get(end) if period == "quarter" else None
    return ExtractedValue(metric_code=code, value=value, unit=unit, scale=scale,
                          period_start=start, period_end=end, period=period,
                          evidence=evidence)  # fmt: skip


def test_candidates_convert_ground_and_filter() -> None:
    mar = {m.code: m for m in REG.for_company("MAR", IND.groups["HOTELS"]).metrics}
    src = "Worldwide (2) $ 138.74 4.1 % 71.6 % 0.8 % pts.\nTotal revenues 7,071 6,744"
    values = [
        ev("revpar", "138.74", "usd", "quarter", "2026-06-30", "Worldwide (2) $ 138.74 4.1 %"),
        ev("occupancy", "71.6", "percent", "quarter", "2026-06-30", "$ 138.74 4.1 % 71.6 %"),
        ev(
            "revenue",
            "7,071",
            "usd",
            "quarter",
            "2026-06-30",
            "Total revenues 7,071 6,744",
            "millions",
        ),
        ev("revpar", "131.14", "usd", "year_to_date", "2026-06-30", "Worldwide (2) $ 138.74"),
        ev(
            "revpar", "999", "usd", "quarter", "2026-06-30", "Worldwide (2) $ 138.74 4.1 %"
        ),  # not in quote
        ev(
            "occupancy", "71.6", "usd", "quarter", "2026-06-30", "$ 138.74 4.1 % 71.6 %"
        ),  # wrong unit
        ev(
            "revpar", "138.74", "usd", "quarter", "2026-06-30", "RevPAR was $138.74"
        ),  # not in source
    ]
    got, rejected = candidates(values, mar, src, DEC)
    assert {(c.metric_code, c.value) for c in got} == {
        ("revpar", 138.74),
        ("occupancy", 0.716),
        ("revenue", 7.071e9),
    }
    assert rejected == 3  # year-to-date is skipped by design, not rejected


def test_dual_check_needs_agreement() -> None:
    a = [Candidate("cet1_ratio", ("Q", 2026, 2), "2026-06-30", 0.142, "q"),
         Candidate("lcr", ("Q", 2026, 2), "2026-06-30", 1.10, "q")]  # fmt: skip
    b = [Candidate("cet1_ratio", ("Q", 2026, 2), "2026-06-30", 0.142, "q"),
         Candidate("lcr", ("Q", 2026, 2), "2026-06-30", 1.12, "q")]  # fmt: skip
    kept, dropped = dual_checked(a, b)
    assert [c.metric_code for c in kept] == ["cet1_ratio"] and dropped == 1


RELEASE = b"""<html><body><p>Marriott Reports Third Quarter 2026 Results</p>
<p>RevPAR for the 2026 third quarter: worldwide comparable systemwide RevPAR was $140.12.</p>
<table><tr><td>Total revenues</td><td>7,300</td><td>6,489</td></tr></table>
<p>($ in millions)</p></body></html>"""


def test_stage_writes_preliminary_and_crosschecks(tmp_path) -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    storage = LocalStorage(tmp_path)
    storage.write_bytes("ex991.htm", RELEASE)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        c.execute(insert(source_document).values(
            company_id=1, source_type="SEC_FILING", form_type="8-K", doc_role="EX-99.1",
            accession_no="acc-1", doc_name="ex991.htm", filed_at="2026-11-04", url="u",
            content_hash="h", raw_path="ex991.htm"))  # fmt: skip
        # XBRL already has Q3 2025 revenue: the release's comparative is cross-checked only.
        c.execute(insert(metric_value).values(
            company_id=1, metric_code="revenue", period_type="Q", fiscal_year=2025,
            fiscal_quarter=3, period_end="2025-09-30", value=6.489e9, value_status="OK",
            unit="USD", derivation="REPORTED", formula="f", source_tags="{}",
            source_accn="x", computed_at=NOW))  # fmt: skip

    def answer(model, user):
        return {"values": [
            {"metric_code": "revenue", "value": "7,300", "unit": "usd", "scale": "millions",
             "period_start": "2026-07-01", "period_end": "2026-09-30", "period": "quarter",
             "evidence": "Total revenues 7,300 6,489"},
            {"metric_code": "revenue", "value": "6,489", "unit": "usd", "scale": "millions",
             "period_start": "2025-07-01", "period_end": "2025-09-30", "period": "quarter",
             "evidence": "Total revenues 7,300 6,489"},
            {"metric_code": "revpar", "value": "140.12", "unit": "usd", "scale": "units",
             "period_start": "2026-07-01", "period_end": "2026-09-30", "period": "quarter",
             "evidence": "worldwide comparable systemwide RevPAR was $140.12"},
        ]}  # fmt: skip

    llm = FakeLLM(answer)
    cfgs = [CompanyConfig(ticker="MAR", cik=1048286)]
    text_cfg = load_text_config(CONFIG_DIR)
    pair = (llm, load_llm_config(CONFIG_DIR))
    for _ in range(2):
        res = run_text_metrics_stage(cfgs, REG, IND, text_cfg, pair, storage, engine,
                                     now=lambda: NOW)  # fmt: skip
        assert res[0].ok, res
    assert len(llm.calls) == 1  # MAR has no dual-check metrics; the rerun is fully cached
    with engine.connect() as c:
        rows = {
            (r.metric_code, r.fiscal_year, r.fiscal_quarter): (r.value, r.derivation)
            for r in c.execute(select(metric_value))
        }
    assert rows == {
        ("revenue", 2025, 3): (6.489e9, "REPORTED"),  # untouched; release agreed with it
        ("revenue", 2026, 3): (7.3e9, "PRELIMINARY"),
        ("revpar", 2026, 3): (140.12, "LLM_EXTRACTED"),
    }


LOAN_TABLE = (
    "Consumer credit portfolio\n(in millions) Jun 30, 2026 Dec 31, 2025 Jun 30, 2026 Dec 31, 2025\n"
    "Residential real estate(a)\n$ 300,664 $ 303,531 $ 3,615 $ 3,632\n"
    "Auto and other(b)(c)\n66,464 65,210 228 243\n"
    "Credit card\nLoans retained(f)\n249,876 247,797 NA NA\n"
    "Wholesale credit portfolio\nLoans retained $ 846,804 $ 792,367 $ 4,191 $ 4,398"
)


def _loan(seg, parent, out, na, end, evidence):
    return {"segment": seg, "parent_segment": parent, "outstanding": out, "nonaccrual": na,
            "scale": "millions", "period_end": end, "evidence": evidence}  # fmt: skip


@pytest.mark.parametrize("total_loans", [None, 1_397_344e6, 1_000_000e6])
def test_loan_book_by_class_with_prior_year_end(tmp_path, total_loans) -> None:
    """With total loans from XBRL, classes adding up to more than it are not stored (D-042)."""
    from uw_pipeline.enrich.sources import load_source_labels
    from uw_pipeline.schema import (
        filing_section,
        loan_portfolio_detail,
        loan_portfolio_segment,
        section_chunk,
    )

    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0000019617", ticker="JPM", name="JPMorgan", legal_name="JPM CHASE",
            industry_group="BANKS", is_financial=True, is_active=True, fiscal_year_end="1231",
            source_url="u", source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        c.execute(insert(source_document).values(
            doc_id=7, company_id=1, source_type="SEC_FILING", form_type="10-Q", doc_role="PRIMARY",
            accession_no="acc-q", doc_name="q.htm", filed_at="2026-08-06", url="https://sec/q.htm",
            content_hash="h"))  # fmt: skip
        c.execute(insert(filing_section).values(
            section_id=1, doc_id=7, section_code="10Q-I-2", start_offset=0,
            char_count=len(LOAN_TABLE), text=LOAN_TABLE, text_hash="h"))  # fmt: skip
        c.execute(insert(section_chunk).values(
            section_id=1, chunk_index=0, start_offset=0, end_offset=len(LOAN_TABLE),
            text=LOAN_TABLE))  # fmt: skip
        if total_loans is not None:
            c.execute(insert(metric_value).values(
                company_id=1, metric_code="total_loans", period_type="Q", fiscal_year=2026,
                fiscal_quarter=2, period_end="2026-06-30", value=total_loans, value_status="OK",
                unit="USD", derivation="REPORTED", formula="f", source_tags="{}",
                source_accn="x", computed_at=NOW))  # fmt: skip

    rre = "Residential real estate(a)\n$ 300,664 $ 303,531 $ 3,615 $ 3,632"
    auto = "66,464 65,210 228 243"
    card = "249,876 247,797 NA NA"
    whs = "Loans retained $ 846,804 $ 792,367 $ 4,191 $ 4,398"

    def answer(model, user):
        if "Metrics to extract" in user:
            return {"values": []}
        rows = [
            _loan("Residential real estate", "Consumer, excluding credit card", "300,664", "3,615", "2026-06-30", rre),
            _loan("Residential real estate", "Consumer, excluding credit card", "303,531", "3,632", "2025-12-31", rre),
            _loan("Auto and other", "Consumer, excluding credit card", "66,464", "228", "2026-06-30", auto),
            _loan("Credit card", "Credit card", "249,876", None, "2026-06-30", card),
            _loan("Wholesale", "Wholesale", "846,804", "4,191", "2026-06-30", whs),
            _loan("Wholesale", "Wholesale", "792,367", "4,398", "2025-12-31", whs),
        ]  # fmt: skip
        if model == load_llm_config(CONFIG_DIR).tiebreaker.model:  # disagrees on auto -> dropped
            rows[2] = _loan("Auto & other", "Consumer", "65,210", "243", "2026-06-30", auto)
            rows[0]["segment"] = "Residential Real Estate (a)"  # same class, other spelling
        return {"segments": rows}

    res = run_text_metrics_stage(
        [CompanyConfig(ticker="JPM", cik=19617)], REG, IND, load_text_config(CONFIG_DIR),
        (FakeLLM(answer), load_llm_config(CONFIG_DIR)), LocalStorage(tmp_path), engine,
        labels=load_source_labels(CONFIG_DIR), now=lambda: NOW,
    )  # fmt: skip
    assert res[0].ok, res
    with engine.connect() as c:
        rows = {(r.period_end, r.segment): r for r in c.execute(select(loan_portfolio_segment))}
        if total_loans == 1_000_000e6:  # classes add up to 1,397,344M: double counting
            assert rows == {}
            return
        detail = c.execute(select(loan_portfolio_detail)).mappings().one()
    assert set(rows) == {
        ("2026-06-30", "Residential real estate"), ("2026-06-30", "Credit card"),
        ("2026-06-30", "Wholesale"), ("2025-12-31", "Residential real estate"),
        ("2025-12-31", "Wholesale"),
    }  # fmt: skip
    rre_now = rows[("2026-06-30", "Residential real estate")]
    assert rre_now.parent_segment == "Consumer, excluding credit card"
    assert rre_now.nonaccrual_ratio == pytest.approx(3615 / 300664)
    assert rows[("2026-06-30", "Credit card")].nonaccrual_ratio is None
    total = 300664 + 249876 + 846804
    assert rows[("2026-06-30", "Wholesale")].pct_of_book == pytest.approx(846804 / total)
    assert detail["period_end"] == "2026-06-30" and detail["prior_period_end"] == "2025-12-31"
    assert "two models" in detail["methodology"]
    import json

    sources = json.loads(detail["data_sources_json"])
    assert sources == [{"title": "JPM CHASE 10-Q filed 2026-08-06 · Part I, Item 2. MD&A",
                        "url": "https://sec/q.htm",
                        "date": "2026-08-06"}]  # fmt: skip
    points = json.loads(detail["raw_data_points_json"])
    assert [p["label"] for p in points] == ["Total", "Wholesale", "Residential real estate",
                                            "Credit card"]  # fmt: skip
    assert "+6.9% vs 2025-12-31" in points[1]["text"]  # 846,804 vs 792,367
    assert points[1]["evidence"] == whs


def test_number_in_quote_needs_whole_numbers() -> None:
    """Review 2026-10-14: a plain substring check let '4.9' pass inside '14.95'."""
    assert not number_in_quote("4.9", "CET1 ratio of 14.95%")
    assert not number_in_quote("14.9", "CET1 ratio of 14.95%")
    assert not number_in_quote("234", "Total loans 1,234 million")
    assert not number_in_quote("1,234", "Total loans 1,234.5 million")
    assert number_in_quote("14.95", "CET1 ratio of 14.95%")
    assert number_in_quote("(0.3)", "71.5 % (0.3) % pts.")
    assert number_in_quote("69.3", "Q2 2026 69.3%")  # neighbouring numbers stay separate
