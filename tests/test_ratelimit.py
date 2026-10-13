import pytest

from tests.conftest import FakeClock
from uw_pipeline.clients.ratelimit import TokenBucket


def test_spaces_requests_at_configured_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=8, clock=clock, sleep=clock.sleep)
    for _ in range(9):
        bucket.acquire()
    # First token is free; the next 8 each wait 1/8 s -> never more than 8 in any second.
    assert clock.now == pytest.approx(1.0)


def test_refills_after_idle() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=8, clock=clock, sleep=clock.sleep)
    bucket.acquire()
    clock.now += 10
    bucket.acquire()
    assert clock.slept == []


def test_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
