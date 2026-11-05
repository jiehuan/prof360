"""Client-credentials wire formats, bounded retries, expiry and safe failure messages."""

import base64
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus

import httpx
import pytest
import respx
from tenacity import wait_none

from tests.conftest import FIXTURES, FakeClock
from uw_pipeline.clients.oauth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    OAuthError,
    OAuthSetupError,
    OAuthTransientError,
)

TOKEN_URL = "https://identity.example.org/oauth/token"
TOKEN_BODY = json.loads((FIXTURES / "oauth/client_credentials_token.json").read_text())
CLIENT_ID = "fixture-client:id +"
CLIENT_SECRET = "fixture-secret:+ /"
SUBSCRIPTION_KEY = "fixture-subscription-key"


@pytest.fixture
def provider():
    clock = FakeClock()
    with httpx.Client() as http:

        def make(**overrides):
            return ClientCredentialsTokenProvider(
                ClientCredentialsConfig(token_url=TOKEN_URL, scope="av.read", **overrides),
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                subscription_key=SUBSCRIPTION_KEY,
                http=http,
                max_attempts=3,
                wait=wait_none(),
                clock=clock,
            )

        yield make, clock


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic", "gateway_headers"])
@pytest.mark.parametrize("subscription_on_token", [True, False])
def test_token_request_formats(provider, method, subscription_on_token):
    make, _ = provider
    client = make(token_auth_method=method, subscription_key_on_token=subscription_on_token)
    with respx.mock() as router:
        route = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        assert client.headers() == {
            "Authorization": f"Bearer {TOKEN_BODY['access_token']}",
            "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
        }
    request = route.calls.last.request
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert not request.url.query
    assert ("Ocp-Apim-Subscription-Key" in request.headers) is subscription_on_token
    if method == "gateway_headers":
        assert request.headers["Client_Id"] == CLIENT_ID
        assert request.headers["Client_Secret"] == CLIENT_SECRET
        assert request.headers["scope"] == "av.read"
        assert "client_id" not in body and "scope" not in body
    else:
        assert body["scope"] == ["av.read"]
        assert "Client_Id" not in request.headers
        if method == "client_secret_post":
            assert body["client_id"] == [CLIENT_ID]
            assert body["client_secret"] == [CLIENT_SECRET]
            assert "Authorization" not in request.headers
        else:
            encoded = request.headers["Authorization"].removeprefix("Basic ")
            assert base64.b64decode(encoded).decode() == (
                f"{quote_plus(CLIENT_ID)}:{quote_plus(CLIENT_SECRET)}"
            )
            assert "client_id" not in body and "client_secret" not in body


@pytest.mark.parametrize("lifetime", [1, 30, 3600])
def test_token_reused_then_renewed_before_actual_expiry(provider, lifetime):
    make, clock = provider
    client = make()
    with respx.mock() as router:
        route = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY | {"expires_in": lifetime})
        assert client.get_token() == TOKEN_BODY["access_token"]
        assert client.get_token() == TOKEN_BODY["access_token"]
        assert route.call_count == 1
        clock.now = lifetime - min(120, lifetime * 0.1)
        client.get_token()
        assert route.call_count == 2
        client.get_token(force=True)
        assert route.call_count == 3


def test_concurrent_token_consumers_share_one_renewal(provider):
    make, _ = provider
    client = make()
    with respx.mock() as router, ThreadPoolExecutor(max_workers=4) as pool:
        route = router.post(TOKEN_URL).respond(200, json=TOKEN_BODY)
        assert (
            list(pool.map(lambda _: client.get_token(), range(8)))
            == [TOKEN_BODY["access_token"]] * 8
        )
        assert route.call_count == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_permanent_errors_and_redirects_are_not_retried_or_exposed(provider, status):
    make, _ = provider
    with respx.mock() as router:
        route = router.post(TOKEN_URL).respond(
            status, text=CLIENT_SECRET, headers={"Location": "https://other.example.org/token"}
        )
        with pytest.raises(OAuthError) as exc:
            make().get_token()
    assert route.call_count == 1 and str(status) in str(exc.value)
    assert CLIENT_SECRET not in str(exc.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_token_errors_have_bounded_retries(provider, status):
    make, _ = provider
    with respx.mock() as router:
        route = router.post(TOKEN_URL).respond(status, text=CLIENT_SECRET)
        with pytest.raises(OAuthTransientError):
            make().get_token()
    assert route.call_count == 3


def test_token_transport_exception_is_sanitized_even_in_traceback(provider):
    make, _ = provider
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(side_effect=httpx.ConnectError(CLIENT_SECRET))
        with pytest.raises(OAuthError) as exc:
            make().get_token()
    assert route.call_count == 3
    assert CLIENT_SECRET not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        TOKEN_BODY | {"access_token": ""},
        TOKEN_BODY | {"access_token": 5},
        TOKEN_BODY | {"access_token": "bad\r\ntoken"},
        TOKEN_BODY | {"expires_in": 0},
        TOKEN_BODY | {"expires_in": -1},
        TOKEN_BODY | {"expires_in": "nan"},
        TOKEN_BODY | {"expires_in": "inf"},
        TOKEN_BODY | {"expires_in": None},
        TOKEN_BODY | {"token_type": "MAC"},
        {"access_token": "without-expiry"},
    ],
)
def test_invalid_token_responses_are_rejected(provider, body):
    make, _ = provider
    with respx.mock() as router:
        router.post(TOKEN_URL).respond(200, json=body)
        with pytest.raises(OAuthSetupError, match="response is invalid"):
            make().get_token()


def test_invalid_json_and_expired_on_arrival_tokens_are_rejected(provider):
    make, clock = provider
    with respx.mock() as router:
        route = router.post(TOKEN_URL).respond(200, text=CLIENT_SECRET)
        with pytest.raises(OAuthSetupError) as exc:
            make().get_token()
        assert CLIENT_SECRET not in "".join(traceback.format_exception(exc.value))

        def delayed_response(request):
            clock.now += 2
            return httpx.Response(200, json=TOKEN_BODY | {"expires_in": 1})

        route.mock(side_effect=delayed_response)
        with pytest.raises(OAuthSetupError, match="expired before receipt"):
            make().get_token()
