"""D-098: raw snapshots older than keep_days are deleted; archive documents, transcripts, the
newest file of each folder and keys a source_document row references are kept."""

from datetime import UTC, date, datetime

import httpx
import respx
from sqlalchemy import create_engine, insert

from tests.conftest import CONFIG_DIR
from uw_pipeline.clients.sec import COMPANY_TICKERS_URL
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.jobs.orchestrator import load_jobs_config
from uw_pipeline.jobs.run_prune import (
    RawRetention,
    company_prefixes,
    expired,
    file_date,
    prune_prefixes,
    run_prune_stage,
)
from uw_pipeline.schema import company, metadata, source_document

NOW = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
CFG = RawRetention(
    keep_days=7,
    company_prefixes=["sec/companyfacts/CIK{cik}", "sec/submissions/CIK{cik}", "av/daily/{symbol}",
                      "av/news/{symbol}"],
    shared_prefixes=["sec/company_tickers"],
)  # fmt: skip
MAR = CompanyConfig(ticker="MAR", cik=1048286)
FACTS = "sec/companyfacts/CIK0001048286"


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0001048286", ticker="MAR", name="Marriott", legal_name="MAR",
            industry_group="HOTELS", is_financial=False, is_active=True, source_url="u",
            source_hash="h", raw_path="p", last_refreshed_at=NOW))  # fmt: skip
    return engine


def _put(st: LocalStorage, *keys: str) -> None:
    for k in keys:
        st.write_bytes(k, b"{}")


def test_file_dates() -> None:
    assert file_date("a/2026-10-18.json") == date(2026, 10, 18)
    assert file_date("a/2026-10-25T035917Z.json") == date(2026, 10, 25)
    assert file_date("av/news/MAR/2026-11-04-from-20261016T0938.json") == date(2026, 11, 4)
    assert file_date("av/transcripts/MAR/2026Q2.json") is None
    assert file_date("sec/filings/CIK1/0001-26-1/mar-20260930.htm") is None


def test_newest_file_of_a_folder_is_kept_however_old() -> None:
    keys = ["x/2026-10-01.json", "x/2026-10-02.json", "y/2026-10-01.json"]
    assert expired(keys, date(2026, 11, 7)) == ["x/2026-10-01.json"]


def test_prefixes_per_company() -> None:
    co = CompanyConfig(ticker="FB", cik=1326801, av_symbols=["META", "FB"])
    assert company_prefixes(CFG, co) == [
        "sec/companyfacts/CIK0001326801", "sec/submissions/CIK0001326801", "av/daily/META",
        "av/daily/FB", "av/news/META", "av/news/FB",
    ]  # fmt: skip


def test_stage_deletes_old_snapshots_only(tmp_path) -> None:
    st = LocalStorage(tmp_path)
    engine = _db()
    old, week, new = "2026-10-30T031659Z.json", "2026-11-07.json", "2026-11-14T043000Z.json"
    kept = [f"{FACTS}/{week}", f"{FACTS}/{new}", "av/daily/MAR/2026-11-14.json",
            "av/transcripts/MAR/2026Q2.json", "sec/filings/CIK0001048286/a/mar.htm",
            "av/news/MAR/2026-10-01-from-20260901T0000.json",  # newest of its folder
            "sec/submissions/CIK0001048286/2026-10-01.json"]  # referenced below  # fmt: skip
    gone = [f"{FACTS}/{old}", "av/daily/MAR/2026-11-01.json",
            "sec/submissions/CIK0001048286/2026-09-01.json"]  # fmt: skip
    _put(st, *kept, *gone, "sec/submissions/CIK0001048286/2026-11-13.json")
    with engine.begin() as c:
        c.execute(insert(source_document).values(
            company_id=1, source_type="SEC_FILING", form_type="X", doc_role="PRIMARY",
            accession_no="a", doc_name="d", filed_at="2026-10-01", url="u",
            raw_path="sec/submissions/CIK0001048286/2026-10-01.json"))  # fmt: skip
    [result] = run_prune_stage([MAR], CFG, st, engine, now=lambda: NOW)
    assert result.ok and result.detail == "deleted 3 of 9", result.detail
    assert all(st.exists(k) for k in kept)
    assert not any(st.exists(k) for k in gone)


def test_dry_run_deletes_nothing(tmp_path) -> None:
    st = LocalStorage(tmp_path)
    _put(st, "sec/company_tickers/2026-10-01.json", "sec/company_tickers/2026-11-14.json")
    stats = prune_prefixes(["sec/company_tickers", "missing/folder"], st, _db(),
                           date(2026, 11, 7), dry_run=True)  # fmt: skip
    assert (stats.files, stats.deleted) == (2, 1)
    assert st.exists("sec/company_tickers/2026-10-01.json")


def test_configured_retention_never_lists_documents_or_transcripts() -> None:
    cfg = load_jobs_config(CONFIG_DIR)
    prefixes = cfg.raw_retention.company_prefixes + cfg.raw_retention.shared_prefixes
    assert prefixes and cfg.raw_retention.keep_days >= 7
    assert not [p for p in prefixes if p.startswith(("sec/filings", "av/transcripts"))]
    for job in ("nightly", "morning"):
        assert cfg.jobs[job].stages[-1] == "prune"


def test_ticker_map_fetched_once_per_client(make_sec_client) -> None:
    with respx.mock() as router:
        route = router.get(COMPANY_TICKERS_URL).mock(
            return_value=httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "x"}})
        )
        sec = make_sec_client()
        assert sec.company_tickers() == sec.company_tickers() == {"X": 1}
        assert route.call_count == 1
