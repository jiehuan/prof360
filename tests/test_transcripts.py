from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from sqlalchemy import create_engine, insert, select
from tenacity import wait_none

from tests.conftest import FIXTURES, FakeClock
from uw_pipeline.clients.alphavantage import AV_URL, AlphaVantageClient
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.jobs.run_transcripts import run_transcripts_stage, transcript_text
from uw_pipeline.schema import company, filing_section, metadata, metric_value, source_document

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MARRIOTT",
            industry_group="HOTELS", is_financial=False, is_active=True, source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        quarters = [(2025, 3, "2025-09-30"), (2025, 4, "2025-12-31"), (2026, 1, "2026-03-31"),
                    (2026, 2, "2026-06-30"), (2025, 2, "2025-06-30")]  # fmt: skip
        c.execute(insert(metric_value), [
            {"company_id": 1, "metric_code": "revenue", "period_type": "Q", "fiscal_year": fy,
             "fiscal_quarter": fq, "period_end": end, "value": 1.0, "value_status": "OK",
             "unit": "USD", "derivation": "REPORTED", "formula": "f", "source_tags": "{}",
             "source_accn": "a", "computed_at": NOW}
            for fy, fq, end in quarters
        ])  # fmt: skip
    return engine


def _av(tmp_path):
    clock = FakeClock()
    return AlphaVantageClient(
        "K", LocalStorage(tmp_path / "raw"),
        rate_limiter=TokenBucket(rate=5 / 60, clock=clock, sleep=clock.sleep),
        daily_quota=None, wait=wait_none(), today=lambda: date(2026, 10, 13),
    )  # fmt: skip


def _routes(router):
    body = (FIXTURES / "av" / "transcript_MAR_2026Q2.json").read_bytes()

    def respond(request):
        quarter = request.url.params["quarter"]
        if quarter == "2026Q2":
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"symbol": "MAR", "quarter": quarter, "transcript": []})

    return router.get(AV_URL).mock(side_effect=respond)


def test_latest_four_quarters_stored_and_rerun_is_free(tmp_path) -> None:
    engine, cfg = _db(), [CompanyConfig(ticker="MAR", cik=1048286)]
    with respx.mock() as router, _av(tmp_path) as av:
        route = _routes(router)
        res = run_transcripts_stage(cfg, av, engine, now=lambda: NOW)
        assert res[0].ok and res[0].detail == "stored=1, already=0, unavailable=3"
        asked = sorted(c.request.url.params["quarter"] for c in route.calls)
        assert asked == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]  # latest 4, not 2025Q2

        res = run_transcripts_stage(cfg, av, engine, now=lambda: NOW + timedelta(days=1))
        assert res[0].detail == "stored=0, already=1, unavailable=3"
        assert route.call_count == 4  # nothing re-requested within a week

        run_transcripts_stage(cfg, av, engine, now=lambda: NOW + timedelta(days=8))
        assert route.call_count == 7  # unpublished quarters retried after 7 days

    with engine.connect() as conn:
        doc = conn.execute(select(source_document)).mappings().one()
        text = conn.execute(select(filing_section.c.text)).scalar_one()
    assert (doc["accession_no"], doc["source_type"], doc["period_end"]) == (
        "AV-MAR-2026Q2", "TRANSCRIPT", "2026-06-30",
    )  # fmt: skip
    assert "apikey" not in doc["url"]
    assert text.startswith("Operator: Hello, and welcome")
    assert "Anthony Capuano (President and Chief Executive Officer): Thanks, Jackie" in text


def test_transcript_text_one_line_per_turn() -> None:
    turns = [{"speaker": "Operator", "title": "Operator", "content": "Welcome.\n Go ahead."},
             {"speaker": "Jane Doe", "title": "CFO", "content": "Thanks."}]  # fmt: skip
    assert transcript_text(turns) == "Operator: Welcome. Go ahead.\nJane Doe (CFO): Thanks."


def test_old_symbol_tried_after_a_ticker_change(tmp_path) -> None:
    """SKYD's calls are filed under PSKY at Alpha Vantage (live check 2026-10-13)."""
    engine = _db()
    cfg = [CompanyConfig(ticker="MAR", cik=1048286, av_symbols=["NEWT", "MAR"])]
    body = (FIXTURES / "av" / "transcript_MAR_2026Q2.json").read_bytes()

    def respond(request):
        if request.url.params["symbol"] == "MAR" and request.url.params["quarter"] == "2026Q2":
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"symbol": request.url.params["symbol"], "transcript": []})

    with respx.mock() as router, _av(tmp_path) as av:
        route = router.get(AV_URL).mock(side_effect=respond)
        res = run_transcripts_stage(cfg, av, engine, now=lambda: NOW)
        assert res[0].detail == "stored=1, already=0, unavailable=3"
        asked = [(c.request.url.params["symbol"], c.request.url.params["quarter"])
                 for c in route.calls]  # fmt: skip
        assert ("NEWT", "2026Q2") in asked and ("MAR", "2026Q2") in asked
    with engine.connect() as conn:
        doc = conn.execute(select(source_document)).mappings().one()
    assert doc["accession_no"] == "AV-MAR-2026Q2" and "symbol=MAR" in doc["url"]
