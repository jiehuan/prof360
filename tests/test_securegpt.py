"""SecureGPT gateway client (Phase 11) against a mocked gateway; no network."""

import json

import httpx
import pytest
import respx

from tests.conftest import CONFIG_DIR
from uw_pipeline.clients.llm import LLMError, LLMSetupError
from uw_pipeline.clients.securegpt import SecureGPTClient, load_securegpt_config
from uw_pipeline.enrich import schemas
from uw_pipeline.enrich.schemas import strict_json_schema

CFG = load_securegpt_config(CONFIG_DIR)
DV = CFG.environments["dv"]
NANO = "gpt-6-luna-2026-09-22"  # the extractor's deployment
CHAT = f"{DV.base_url}/{NANO}/chat/completions"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _client(clock: Clock, **overrides) -> SecureGPTClient:
    cfg = CFG.model_copy(update=overrides)
    return SecureGPTClient(cfg, env="dv", client_id="id", client_secret="secret",
                           subscription_key="subkey", sleep=clock.sleep, clock=clock)  # fmt: skip


def _ok(content: dict, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "model": NANO,
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    })  # fmt: skip


def _call(c: SecureGPTClient, model: str = NANO, effort: str = "low"):
    return c.complete_json(model=model, effort=effort, max_tokens=1000, system="sys",
                           user="hello", schema={"type": "object"})  # fmt: skip


def test_token_then_structured_completion() -> None:
    clock = Clock()
    with respx.mock(assert_all_called=True) as r:
        token = r.post(DV.token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "T1", "expires_in": 3600})
        )
        chat = r.post(CHAT).mock(return_value=_ok({"answer": 1}))
        res = _call(_client(clock))
    assert res.output == {"answer": 1} and (res.input_tokens, res.output_tokens) == (120, 30)
    th = token.calls[0].request.headers
    assert (th["Client_Id"], th["Client_Secret"], th["scope"], th["Ocp-Apim-Subscription-Key"]) == (
        "id", "secret", "urn:grp:chatgpt", "subkey")  # fmt: skip
    req = chat.calls[0].request
    assert req.url.params["api-version"] == "2025-04-01-preview"
    assert req.headers["Authorization"] == "Bearer T1"
    body = json.loads(req.content)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 1000
    assert "reasoning_effort" not in body  # off until the smoke test confirms the gateway


def test_token_is_reused_and_refreshed_before_expiry() -> None:
    clock = Clock()
    c = _client(clock)
    with respx.mock() as r:
        token = r.post(DV.token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 600})
        )
        r.post(CHAT).mock(return_value=_ok({}))
        _call(c)
        _call(c)
        assert token.call_count == 1
        clock.t += 600  # past expiry minus the margin
        _call(c)
        assert token.call_count == 2


def test_expired_token_401_refreshes_once() -> None:
    clock = Clock()
    with respx.mock() as r:
        r.post(DV.token_url).mock(side_effect=[
            httpx.Response(200, json={"access_token": "OLD"}),
            httpx.Response(200, json={"access_token": "NEW"}),
        ])  # fmt: skip
        chat = r.post(CHAT).mock(side_effect=[httpx.Response(401), _ok({"x": 1})])
        assert _call(_client(clock)).output == {"x": 1}
    assert chat.calls[1].request.headers["Authorization"] == "Bearer NEW"


def test_429_retries_after_the_gateway_says() -> None:
    clock = Clock()
    with respx.mock() as r:
        r.post(DV.token_url).mock(return_value=httpx.Response(200, json={"access_token": "T"}))
        r.post(CHAT).mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(503), _ok({"x": 1}),
        ])  # fmt: skip
        assert _call(_client(clock)).output == {"x": 1}
    assert clock.slept == [7.0, CFG.backoff_initial * 2]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_ok({}, finish="length"), LLMError),
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": None, "refusal": "I can't help with that."},
                        }
                    ]
                },
            ),
            LLMError,
        ),
        (
            httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "content_filter", "message": {"content": None}}]
                },
            ),
            LLMError,
        ),
        (
            httpx.Response(
                400,
                json={"error": {"code": "content_filter", "message": "The prompt was filtered"}},
            ),
            LLMError,
        ),
        (
            httpx.Response(
                400,
                json={"error": {"code": "unsupported_parameter", "message": "reasoning_effort"}},
            ),
            LLMSetupError,
        ),
        (httpx.Response(403, json={"error": {"message": "forbidden"}}), LLMSetupError),
        (
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "maximum context length is 400000",
                    }
                },
            ),
            LLMError,  # one oversized request, not a setup problem
        ),
    ],
)
def test_failures_are_classified(response, error) -> None:
    """Item-level problems (LLMError) are recorded and skipped; setup problems
    (LLMSetupError) stop LLM use for the run."""
    with respx.mock() as r:
        r.post(DV.token_url).mock(return_value=httpx.Response(200, json={"access_token": "T"}))
        r.post(CHAT).mock(return_value=response)
        with pytest.raises(error):
            _call(_client(Clock()))


def test_rejected_credentials_and_unknown_model_are_setup_errors() -> None:
    with respx.mock() as r:
        r.post(DV.token_url).mock(return_value=httpx.Response(401))
        with pytest.raises(LLMSetupError):
            _call(_client(Clock()))
    with pytest.raises(LLMSetupError):
        _call(_client(Clock()), model="gpt-unknown")


def test_reasoning_effort_sent_when_enabled() -> None:
    with respx.mock() as r:
        r.post(DV.token_url).mock(return_value=httpx.Response(200, json={"access_token": "T"}))
        chat = r.post(CHAT).mock(return_value=_ok({}))
        _call(_client(Clock(), send_reasoning_effort=True), effort="xhigh")
    assert json.loads(chat.calls[0].request.content)["reasoning_effort"] == "high"


ALLOWED = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
           "anyOf", "$ref", "$defs", "description"}  # fmt: skip


def _check_strict(node, path="$"):
    if isinstance(node, dict):
        extra = set(node) - ALLOWED
        assert not extra, f"{path}: keywords not allowed in OpenAI strict mode: {extra}"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert set(node.get("required", [])) == set(node.get("properties", {})), path
        for k, v in node.items():
            if k in ("properties", "$defs"):
                for name, sub in v.items():
                    _check_strict(sub, f"{path}.{name}")
            elif isinstance(v, dict | list):
                _check_strict(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, x in enumerate(node):
            _check_strict(x, f"{path}[{i}]")


def test_every_output_schema_is_valid_in_openai_strict_mode() -> None:
    from pydantic import BaseModel

    from uw_pipeline.enrich.news_events import EventGroups
    from uw_pipeline.enrich.news_relevance import Verdicts

    models = [
        m for m in vars(schemas).values()
        if isinstance(m, type) and issubclass(m, BaseModel) and m is not BaseModel
    ]  # fmt: skip
    models += [EventGroups, Verdicts]
    assert len(models) > 20
    for m in models:
        schema = strict_json_schema(m)
        assert schema.get("type") == "object", m.__name__
        _check_strict(schema, m.__name__)
