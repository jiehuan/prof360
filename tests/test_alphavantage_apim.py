"""AV routing, cache/quota behavior, credential preflight and stage integration."""

import json
import shutil
import traceback

import httpx
import pytest
import respx
import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine
from tenacity import wait_none
from typer.testing import CliRunner

from tests.conftest import CONFIG_DIR, FIXTURES, TEST_UA, FakeClock
from uw_pipeline.cli import app
from uw_pipeline.clients.alphavantage import (
    AV_URL,
    AlphaVantageClient,
    AlphaVantageError,
    AlphaVantageSetupError,
    QuotaExhaustedError,
    load_alphavantage_config,
    make_alphavantage_client,
    public_url,
    validate_alphavantage_settings,
)
from uw_pipeline.clients.oauth import ClientCredentialsConfig, ClientCredentialsTokenProvider
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.jobs.stages import StageRunner
from uw_pipeline.settings import Settings

QUERY_URL = "https://av-gateway.example.org/av/query"
TOKEN_URL = "https://identity.example.org/oauth/token"
KEY = "fixture-private-av-key"
CLIENT_ID = "fixture-private-client-id"
CLIENT_SECRET = "fixture-private-client-secret"
SUBSCRIPTION_KEY = "fixture-private-subscription-key"
TOKEN_BODY = json.loads((FIXTURES / "oauth/client_credentials_token.json").read_text())
PAYLOAD = json.loads((FIXTURES / "av/market/earnings_F.json").read_text())
ENV = {"query_url": QUERY_URL, "oauth": {"token_url": TOKEN_URL, "scope": "av.read"}}


@pytest.fixture
def apim(tmp_path):
    clock = FakeClock()
    http = httpx.Client()
    oauth = ClientCredentialsTokenProvider(
        ClientCredentialsConfig.model_validate(ENV["oauth"]),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        subscription_key=SUBSCRIPTION_KEY,
        http=http,
        wait=wait_none(),
        clock=clock,
    )
    with AlphaVantageClient(
        KEY,
        LocalStorage(tmp_path),
        query_url=QUERY_URL,
        oauth=oauth,
        http=http,
        rate_limiter=TokenBucket(100, clock=clock, sleep=clock.sleep),
        wait=wait_none(),
        daily_quota=10,
    ) as client:
        yield client, clock


def test_apim_auth_parameters_cache_and_public_lineage(apim, tmp_path):
    client, _ = apim
    with respx.mock() as router:
        token = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        query = router.get(QUERY_URL).respond(200, json=PAYLOAD)
        payload, key = client.earnings("F")
        assert payload == PAYLOAD and client.earnings("F") == (payload, key)
        assert token.call_count == query.call_count == 1
        assert client.calls_today() == 1  # token request and raw cache hit are free
        request = query.calls.last.request
        assert dict(request.url.params) == {"function": "EARNINGS", "symbol": "F", "apikey": KEY}
        assert request.headers["Authorization"] == f"Bearer {TOKEN_BODY['access_token']}"
        assert request.headers["Ocp-Apim-Subscription-Key"] == SUBSCRIPTION_KEY
        client.earnings("OTHER")
        assert token.call_count == 1 and query.call_count == 2  # token reused across data fetches
    url = public_url({"function": "EARNINGS", "symbol": "F", "apikey": KEY})
    assert url.startswith(AV_URL) and "apikey" not in url and "gateway" not in url
    for path in tmp_path.rglob("*.json"):
        assert all(
            value not in path.read_text()
            for value in (
                KEY,
                CLIENT_ID,
                CLIENT_SECRET,
                SUBSCRIPTION_KEY,
                TOKEN_BODY["access_token"],
            )
        )


def test_preexisting_cache_does_not_acquire_a_token(apim, tmp_path):
    client, _ = apim
    key = "av/previous.json"
    client._storage.write_bytes(key, json.dumps(PAYLOAD).encode())
    with respx.mock():  # any HTTP request fails
        assert client.fetch({"function": "EARNINGS"}, key) == PAYLOAD
    assert client.calls_today() == 0


@pytest.mark.parametrize("second_status", [200, 401, 403])
def test_401_refreshes_and_replays_only_once(apim, second_status):
    client, _ = apim
    with respx.mock() as router:
        token = router.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json=TOKEN_BODY),
                httpx.Response(200, json=TOKEN_BODY | {"access_token": "fixture-renewed-token"}),
            ]
        )
        query = router.get(QUERY_URL).mock(
            side_effect=[httpx.Response(401), httpx.Response(second_status, json=PAYLOAD)]
        )
        if second_status == 200:
            assert client.earnings("F")[0] == PAYLOAD
        else:
            with pytest.raises(AlphaVantageError, match=str(second_status)):
                client.earnings("F")
        assert token.call_count == query.call_count == client.calls_today() == 2
        assert query.calls[1].request.headers["Authorization"] == "Bearer fixture-renewed-token"


@pytest.mark.parametrize("status", [403, 404, 302])
def test_other_permanent_statuses_do_not_renew_or_follow_redirects(apim, status):
    client, _ = apim
    with respx.mock() as router:
        token = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        query = router.get(QUERY_URL).respond(
            status, text=CLIENT_SECRET, headers={"Location": "https://other.example.org/query"}
        )
        with pytest.raises(AlphaVantageError, match=str(status)):
            client.earnings("F")
        assert token.call_count == query.call_count == 1


def test_failed_token_never_calls_data_or_consumes_av_quota(apim):
    client, _ = apim
    with respx.mock() as router:
        router.post(TOKEN_URL).respond(401, text=CLIENT_SECRET)
        with pytest.raises(AlphaVantageError, match="OAuth rejected credentials"):
            client.earnings("F")
        assert client.calls_today() == 0


def test_quota_blocks_requests_before_token_acquisition_or_forced_renewal(apim):
    client, _ = apim
    client._quota = 1
    with respx.mock() as router:
        token = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        query = router.get(QUERY_URL).respond(401)
        with pytest.raises(QuotaExhaustedError):
            client.earnings("F")
        with pytest.raises(QuotaExhaustedError):
            client.earnings("F")
        assert token.call_count == query.call_count == client.calls_today() == 1


@pytest.mark.parametrize("status", [429, 503])
def test_data_retries_are_bounded_counted_and_reuse_token(apim, status):
    client, _ = apim
    with respx.mock() as router:
        token = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        query = router.get(QUERY_URL).respond(status, text=KEY)
        with pytest.raises(AlphaVantageError, match=str(status)):
            client.earnings("F")
        assert query.call_count == client.calls_today() == 3
        assert token.call_count == 1


@pytest.mark.parametrize(
    "kind", ["transport", "http", "av_error", "echo", "invalid_json", "oauth_error"]
)
def test_credentials_never_escape_failures_or_land_in_raw(apim, tmp_path, kind):
    client, _ = apim
    sensitive = (KEY, CLIENT_ID, CLIENT_SECRET, SUBSCRIPTION_KEY, TOKEN_BODY["access_token"])
    echo = " / ".join(sensitive)
    with respx.mock() as router:
        router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        route = router.get(QUERY_URL)
        if kind == "transport":
            route.mock(side_effect=httpx.ConnectError(echo))
        elif kind == "http":
            route.respond(403, text=echo)
        elif kind == "av_error":
            route.respond(200, json={"Information": echo})
        elif kind == "echo":
            route.respond(200, json={"debug": echo})
        elif kind == "oauth_error":
            route.respond(200, json={"error": "invalid_token", "error_description": echo})
        else:
            route.respond(200, text=echo)
        with pytest.raises(AlphaVantageError) as exc:
            client.earnings("F")
    rendered = "".join(traceback.format_exception(exc.value))
    assert all(value not in rendered for value in sensitive)
    assert all("_quota" in str(path) for path in tmp_path.rglob("*.json"))


def settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        sec_user_agent=TEST_UA,
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        alphavantage_mode="apim",
        alphavantage_api_key=KEY,
        alphavantage_client_id=CLIENT_ID,
        alphavantage_client_secret=CLIENT_SECRET,
        alphavantage_subscription_key=SUBSCRIPTION_KEY,
        **overrides,
    )


def write_config(tmp_path, **overrides):
    raw = {"apim": {"default_env": "runtime", "environments": {"runtime": ENV}}} | overrides
    (tmp_path / "alphavantage.yaml").write_text(yaml.safe_dump(raw))
    return load_alphavantage_config(tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "alphavantage_api_key",
        "alphavantage_client_id",
        "alphavantage_client_secret",
        "alphavantage_subscription_key",
    ],
)
@pytest.mark.parametrize("value", [None, "", "  ", "REPLACE_WITH_CREDENTIAL"])
def test_each_apim_credential_is_required_without_exposing_values(tmp_path, name, value):
    cfg = write_config(tmp_path)
    s = settings(tmp_path).model_copy(update={name: value})
    with pytest.raises(AlphaVantageSetupError, match=name.upper()) as exc:
        validate_alphavantage_settings(s, cfg)
    assert KEY not in str(exc.value) and CLIENT_SECRET not in str(exc.value)


def test_selected_environment_and_placeholder_validation(tmp_path):
    cfg = write_config(tmp_path)
    s = settings(tmp_path)
    assert validate_alphavantage_settings(s, cfg).query_url == QUERY_URL
    with pytest.raises(AlphaVantageSetupError, match="ALPHAVANTAGE_ENV"):
        validate_alphavantage_settings(s.model_copy(update={"alphavantage_env": "unknown"}), cfg)
    cfg = load_alphavantage_config(CONFIG_DIR)
    with pytest.raises(AlphaVantageSetupError, match="placeholders"):
        validate_alphavantage_settings(s, cfg)


def test_legacy_direct_without_yaml_and_factory_apim_routing(tmp_path):
    s = settings(tmp_path).model_copy(update={"alphavantage_mode": "direct"})
    with respx.mock() as router:
        direct = router.get(AV_URL).respond(200, json=PAYLOAD)
        with make_alphavantage_client(s, LocalStorage(tmp_path / "direct")) as client:
            client.earnings("F")
        assert "Authorization" not in direct.calls.last.request.headers
        assert "Ocp-Apim-Subscription-Key" not in direct.calls.last.request.headers
    with pytest.raises(AlphaVantageSetupError, match="ALPHAVANTAGE_ENV"):
        make_alphavantage_client(settings(tmp_path), LocalStorage(tmp_path))
    write_config(tmp_path)
    with respx.mock() as router:
        router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        query = router.get(QUERY_URL).respond(200, json=PAYLOAD)
        with make_alphavantage_client(
            settings(tmp_path), LocalStorage(tmp_path / "apim")
        ) as client:
            client.earnings("F")
        assert query.calls.last.request.url.params["apikey"] == KEY


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.example.org/query",
        "/query",
        "https://id:secret@gateway.example.org/query",
        "https://gateway.example.org/query?apikey=private",
        "https://gateway.example.org/query#secret",
    ],
)
def test_invalid_endpoints_are_rejected_without_echoing_input(tmp_path, url):
    with pytest.raises(AlphaVantageSetupError, match="query_url") as exc:
        write_config(tmp_path, direct={"query_url": url})
    assert url not in str(exc.value)


def test_config_rejects_bad_auth_method_unknown_fields_and_duplicate_keys(tmp_path):
    for raw in (
        {
            "apim": {
                "environments": {
                    "dv": ENV | {"oauth": ENV["oauth"] | {"token_auth_method": "unknown"}}
                }
            }
        },
        {"typo": True},
        {"timeout_sec": 0},
        {"max_attempts": 0},
    ):
        with pytest.raises(AlphaVantageSetupError):
            write_config(tmp_path, **raw)
    (tmp_path / "alphavantage.yaml").write_text("max_attempts: 2\nmax_attempts: 3\n")
    with pytest.raises(AlphaVantageSetupError, match="cannot read"):
        load_alphavantage_config(tmp_path)


def test_settings_mode_is_typed_and_config_check_is_offline(tmp_path, monkeypatch):
    with pytest.raises(ValidationError, match="alphavantage_mode"):
        Settings.model_validate({"sec_user_agent": TEST_UA, "alphavantage_mode": "auto"})
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with respx.mock():
        assert CliRunner().invoke(app, ["config", "check"]).exit_code == 0
        (tmp_path / "config/alphavantage.yaml").write_text("max_attempts: 0\n")
        result = CliRunner().invoke(app, ["config", "check"])
        assert result.exit_code == 1 and "max_attempts" in result.output


def test_av_stage_reports_configuration_errors_but_sec_only_is_lazy(tmp_path):
    s = settings(tmp_path).model_copy(
        update={"config_dir": CONFIG_DIR, "alphavantage_client_secret": None}
    )
    engine = create_engine("sqlite://")
    try:
        with StageRunner(s, engine) as runner:
            # Constructing the runner and selecting a SEC stage does not require AV auth.
            assert runner._stage("company") and runner._av is None
            result = runner("market", "ENPH")
            assert not result.ok and "ALPHAVANTAGE_CLIENT_SECRET" in result.detail
    finally:
        engine.dispose()


def test_public_demo_key_can_appear_in_public_content(tmp_path):
    with respx.mock() as router, AlphaVantageClient("demo", LocalStorage(tmp_path)) as client:
        router.get(AV_URL).respond(200, json={"transcript": "We demonstrated strong growth."})
        assert client.transcript("IBM", "2024Q1")[0]["transcript"]


def test_parallel_apim_requests_overlap_and_share_one_token(tmp_path):
    """D-085 port: the news stage calls from several threads (D-079). Only the quota check and
    the count are locked; the requests themselves overlap, and the workers share one token."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    http = httpx.Client()
    oauth = ClientCredentialsTokenProvider(
        ClientCredentialsConfig.model_validate(ENV["oauth"]), client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET, subscription_key=SUBSCRIPTION_KEY, http=http,
        wait=wait_none(),
    )  # fmt: skip
    lock, now, peak = threading.Lock(), [0], [0]

    def slow(request):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.1)
        with lock:
            now[0] -= 1
        return httpx.Response(200, json=PAYLOAD)

    with AlphaVantageClient(
        KEY, LocalStorage(tmp_path), query_url=QUERY_URL, oauth=oauth, http=http,
        rate_limiter=TokenBucket(rate=1000), wait=wait_none(), daily_quota=None,
    ) as client, respx.mock() as router:  # fmt: skip
        token = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        router.get(QUERY_URL).mock(side_effect=slow)
        oauth.get_token()  # the first worker's token, as in a run
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(client.earnings, ["F", "GM", "MAR", "JPM"]))
        assert token.call_count == 1 and client.calls_today() == 4
    assert peak[0] > 1
