from datetime import date

import httpx
import pytest
import respx

from uw_pipeline.clients.sec import COMPANY_TICKERS_URL, SEC_DATA, pad_cik

MAR_URL = f"{SEC_DATA}/submissions/CIK0001048286.json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1048286, "0001048286"), ("19617", "0000019617"), ("CIK0000092380", "0000092380")],
)
def test_pad_cik(raw: int | str, expected: str) -> None:
    assert pad_cik(raw) == expected


@pytest.mark.parametrize("bad", ["12345678901", "abc", "", "-1"])
def test_pad_cik_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        pad_cik(bad)


def test_sends_user_agent(sec_routes: respx.MockRouter, make_sec_client) -> None:
    make_sec_client().submissions(1048286)
    request = sec_routes.calls.last.request
    assert request.headers["User-Agent"] == "Test Runner test@example.com"


def test_ticker_map(sec_routes: respx.MockRouter, make_sec_client) -> None:
    tickers = make_sec_client().company_tickers()
    assert tickers["MAR"] == 1048286
    assert tickers["PARA"] == 1826011  # reassigned ticker; see companies.yaml


def test_lands_raw_and_reuses_same_day_cache(
    sec_routes: respx.MockRouter, make_sec_client, tmp_path
) -> None:
    client = make_sec_client()
    first = client.submissions(1048286)
    second = client.submissions(1048286)
    assert first.raw_path == second.raw_path == "sec/submissions/CIK0001048286/2026-10-13.json"
    assert (tmp_path / "raw" / first.raw_path).is_file()
    assert (first.from_cache, second.from_cache) == (False, True)
    assert first.content_hash == second.content_hash
    assert sum(1 for c in sec_routes.calls if str(c.request.url) == MAR_URL) == 1


def test_force_and_new_day_refetch(sec_routes: respx.MockRouter, make_sec_client) -> None:
    make_sec_client().submissions(1048286)
    make_sec_client().submissions(1048286, force=True)
    make_sec_client(day=date(2026, 10, 14)).submissions(1048286)
    assert sum(1 for c in sec_routes.calls if str(c.request.url) == MAR_URL) == 3


def test_retries_on_429_then_succeeds(make_sec_client) -> None:
    with respx.mock() as router:
        route = router.get(COMPANY_TICKERS_URL).mock(
            side_effect=[
                httpx.Response(429),
                httpx.ConnectError("boom"),
                httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "x", "title": "X"}}),
            ]
        )
        assert make_sec_client().company_tickers() == {"X": 1}
        assert route.call_count == 3


def test_gives_up_after_max_attempts(make_sec_client) -> None:
    with respx.mock() as router:
        route = router.get(COMPANY_TICKERS_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(Exception, match="503"):
            make_sec_client(max_attempts=3).company_tickers()
        assert route.call_count == 3


def test_does_not_retry_404(make_sec_client) -> None:
    with respx.mock() as router:
        route = router.get(MAR_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            make_sec_client().submissions(1048286)
        assert route.call_count == 1
