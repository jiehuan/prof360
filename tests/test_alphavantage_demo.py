"""The demo smoke check must validate content, stay isolated and honour caching."""

import json

import httpx
import respx
from scripts import alphavantage_demo as demo

from tests.conftest import FIXTURES, FakeClock
from uw_pipeline.clients.alphavantage import AV_URL, AlphaVantageClient
from uw_pipeline.clients.ratelimit import TokenBucket


def setup_client(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        demo,
        "AlphaVantageClient",
        lambda key, storage: AlphaVantageClient(
            key,
            storage,
            rate_limiter=TokenBucket(rate=5 / 60, clock=clock, sleep=clock.sleep),
        ),
    )


def responses():
    return {
        params["function"]: json.loads((FIXTURES / "av/demo" / f"{name}.json").read_text())
        for name, params in demo.EXAMPLES.items()
    }


def test_demo_uses_public_examples_parses_and_reuses_cache(tmp_path, monkeypatch):
    setup_client(monkeypatch)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("ALPHAVANTAGE_API_KEY=PRIVATE_DO_NOT_USE\n")
    bodies = responses()

    def respond(request):
        assert request.url.params["apikey"] == "demo"
        return httpx.Response(200, json=bodies[request.url.params["function"]])

    report = tmp_path / "report.json"
    args = ["--raw-dir", str(tmp_path / "raw"), "--report", str(report)]
    with respx.mock() as router:
        route = router.get(AV_URL).mock(side_effect=respond)
        assert demo.main(args) == 0
        first = json.loads(report.read_text())
        assert first["mode"] == "official_demo" and first["http_requests"] == 4
        assert [r["ok"] for r in first["checks"]] == [True] * 4
        news = next(r for r in first["checks"] if r["check"] == "news")
        assert (news["articles"], news["accepted"], news["filtered"]) == (2, 1, 1)
        assert demo.main(args) == 0 and route.call_count == 4
        assert json.loads(report.read_text())["http_requests"] == 0
    assert env.read_text() == "ALPHAVANTAGE_API_KEY=PRIVATE_DO_NOT_USE\n"
    assert not list(tmp_path.rglob("*.db"))


def test_http_200_with_wrong_company_is_a_failed_check(tmp_path, monkeypatch):
    setup_client(monkeypatch)
    bodies = responses()
    bodies["EARNINGS"]["symbol"] = "WRONG_COMPANY"
    report = tmp_path / "report.json"
    with respx.mock() as router:
        router.get(AV_URL).mock(
            side_effect=lambda request: httpx.Response(
                200, json=bodies[request.url.params["function"]]
            )
        )
        assert demo.main(["--raw-dir", str(tmp_path / "raw"), "--report", str(report)]) == 1
    checks = json.loads(report.read_text())["checks"]
    earnings = next(r for r in checks if r["check"] == "earnings")
    assert not earnings["ok"] and "not IBM" in earnings["detail"]
    assert sum(r["ok"] for r in checks) == 3
