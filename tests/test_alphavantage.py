from datetime import date, datetime

import httpx
import pytest
import respx
from tenacity import wait_none

from tests.conftest import FIXTURES, FakeClock
from uw_pipeline.clients.alphavantage import (
    AV_URL,
    AlphaVantageClient,
    PremiumRequiredError,
    QuotaExhaustedError,
    classify,
    public_url,
)
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import LocalStorage

KEY = "TESTKEY123"


@pytest.fixture
def av(tmp_path):
    clock = FakeClock()
    client = AlphaVantageClient(
        KEY,
        LocalStorage(tmp_path / "raw"),
        rate_limiter=TokenBucket(rate=5 / 60, clock=clock, sleep=clock.sleep),
        daily_quota=3,
        wait=wait_none(),
        today=lambda: date(2026, 10, 13),
    )
    yield client, tmp_path
    client.close()


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"Information": "Thank you for using Alpha Vantage! This is a premium endpoint."},
         PremiumRequiredError),
        ({"Information": "Our standard API rate limit is 25 requests per day."},
         QuotaExhaustedError),
        ({"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute"},
         QuotaExhaustedError),
    ],
)  # fmt: skip
def test_classify_error_bodies(body, error) -> None:
    with pytest.raises(error):
        classify(body)


def test_daily_limit_message_with_premium_mention_is_quota_and_key_is_masked() -> None:
    body = {"Information": f"We have detected your API key as {KEY} and our standard API rate "
            "limit is 25 requests per day. Please subscribe to any of the premium plans."}  # fmt: skip
    with pytest.raises(QuotaExhaustedError) as exc:
        classify(body, KEY)
    assert KEY not in str(exc.value) and "***" in str(exc.value)


def test_data_body_is_not_an_error() -> None:
    classify({"symbol": "MAR", "quarter": "2026Q2", "transcript": []})


def test_transcript_landed_without_key_and_reused(av) -> None:
    client, tmp = av
    body = (FIXTURES / "av" / "transcript_MAR_2026Q2.json").read_bytes()
    with respx.mock() as router:
        route = router.get(AV_URL).mock(return_value=httpx.Response(200, content=body))
        payload, key = client.transcript("MAR", "2026Q2")
        again, _ = client.transcript("MAR", "2026Q2")
    assert route.call_count == 1 and payload == again
    assert route.calls.last.request.url.params["apikey"] == KEY
    assert key == "av/transcripts/MAR/2026Q2.json"
    landed = list((tmp / "raw").rglob("*.json"))
    assert all(KEY not in p.read_text() and KEY not in str(p) for p in landed)
    assert KEY not in public_url({"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": "MAR"})


def test_empty_transcript_is_not_cached(av) -> None:
    client, _ = av
    empty = {"symbol": "MAR", "quarter": "2026Q3", "transcript": []}
    with respx.mock() as router:
        route = router.get(AV_URL).mock(return_value=httpx.Response(200, json=empty))
        client.transcript("MAR", "2026Q3")
        client.transcript("MAR", "2026Q3")
    assert route.call_count == 2


def test_daily_quota_is_enforced_and_persisted(av) -> None:
    client, _ = av
    with respx.mock() as router:
        router.get(AV_URL).mock(return_value=httpx.Response(200, json={"transcript": []}))
        for q in ("2025Q1", "2025Q2", "2025Q3"):
            client.transcript("MAR", q)
        with pytest.raises(QuotaExhaustedError):
            client.transcript("MAR", "2025Q4")
    assert client.calls_today() == 3


def test_premium_error_is_raised_not_landed(av) -> None:
    client, tmp = av
    msg = {"Information": "This is a premium endpoint. You may subscribe..."}
    with respx.mock() as router:
        router.get(AV_URL).mock(return_value=httpx.Response(200, json=msg))
        with pytest.raises(PremiumRequiredError):
            client.news("MAR", datetime(2026, 9, 1))
    assert not (tmp / "raw" / "av" / "news").exists()
