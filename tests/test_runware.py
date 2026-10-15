"""Runware gateway client (D-047) against a mocked gateway; no network."""

import json

import httpx
import pytest
import respx

from tests.conftest import CONFIG_DIR
from uw_pipeline.clients.llm import LLMError, LLMSetupError
from uw_pipeline.clients.runware import RunwareClient, load_runware_config
from uw_pipeline.enrich.llm import load_llm_config

CFG = load_runware_config(CONFIG_DIR)
CHAT = f"{CFG.base_url}/chat/completions"
FLASH = "deepseek:v4@flash"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}  # fmt: skip


def _client(slept: list[float] | None = None, **overrides) -> RunwareClient:
    return RunwareClient(CFG.model_copy(update=overrides), api_key="rw-test-key",
                         sleep=(slept if slept is not None else []).append)  # fmt: skip


def _ok(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "DeepSeek-V4-Flash-0731",
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 90, "completion_tokens": 12, "cost": 0.00001},
    })  # fmt: skip


def _call(c: RunwareClient, model: str = FLASH):
    return c.complete_json(model=model, effort="low", max_tokens=500, system="sys",
                           user="hello", schema=SCHEMA)  # fmt: skip


def test_strict_schema_request_and_result() -> None:
    with respx.mock(assert_all_called=True) as r:
        chat = r.post(CHAT).mock(return_value=_ok('{"answer": "present"}'))
        res = _call(_client())
    assert res.output == {"answer": "present"}
    assert (res.model, res.input_tokens, res.output_tokens) == ("DeepSeek-V4-Flash-0731", 90, 12)
    req = chat.calls[0].request
    assert req.headers["Authorization"] == "Bearer rw-test-key"
    body = json.loads(req.content)
    assert body["model"] == FLASH and body["max_tokens"] == 500
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "output", "strict": True, "schema": SCHEMA},
    }
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hello"}]  # fmt: skip
    assert body["reasoning_effort"] == "low"  # accepted in the smoke test (config)


def test_json_mode_puts_the_schema_in_the_system_prompt_and_strips_a_fence() -> None:
    c = _client(output={FLASH: "json_object"}, reasoning_effort_models=[FLASH])
    with respx.mock() as r:
        chat = r.post(CHAT).mock(return_value=_ok('```json\n{"answer": "none"}\n```'))
        assert _call(c).output == {"answer": "none"}
    body = json.loads(chat.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"].startswith("sys\n\nReturn only one JSON object")
    assert json.dumps(SCHEMA, separators=(",", ":")) in body["messages"][0]["content"]
    assert body["reasoning_effort"] == "low"


def test_openai_models_get_max_completion_tokens() -> None:
    with respx.mock() as r:
        chat = r.post(CHAT).mock(return_value=_ok('{"answer": "x"}'))
        _call(_client(), model="openai:gpt@6-luna")
    body = json.loads(chat.calls[0].request.content)
    assert body["max_completion_tokens"] == 500 and "max_tokens" not in body


def test_retries_transient_errors_then_gives_up() -> None:
    slept: list[float] = []
    with respx.mock() as r:
        r.post(CHAT).mock(side_effect=[httpx.Response(429, headers={"retry-after": "7"}),
                                       _ok('{"answer": "x"}')])  # fmt: skip
        assert _call(_client(slept)).output == {"answer": "x"}
    assert slept == [7.0]
    with respx.mock() as r:
        r.post(CHAT).mock(return_value=httpx.Response(503))
        with pytest.raises(LLMError, match="503"):  # after the last retry
            _call(_client([], max_attempts=2))


@pytest.mark.parametrize(
    ("status", "message", "error"),
    [
        (400, "External inference tasks require a paid invoice or at least $5 credit.",
         LLMSetupError),
        (401, "Invalid API key", LLMSetupError),
        (400, "response_format json_schema is not supported for this model", LLMSetupError),
        (400, "This model's maximum context length is 1000000 tokens", LLMError),
        (400, "messages[1].content is malformed", LLMError),
        (500, "internal", LLMError),
    ],
)  # fmt: skip
def test_errors_are_classified(status: int, message: str, error: type) -> None:
    with respx.mock() as r:
        r.post(CHAT).mock(
            return_value=httpx.Response(
                status, json={"error": {"message": message, "type": "invalid_request_error"}}
            )
        )
        with pytest.raises(error) as exc:
            _call(_client([], max_attempts=1))
    assert "rw-test-key" not in str(exc.value)  # the key never reaches a message


@pytest.mark.parametrize(
    "content",
    [
        '{...}</think>{"answer": "present"}',  # a preview of the reasoning, then the answer
        '{\n  "answer": "none"\n}</think>{\n  "answer": "present"\n}',  # a draft, then the answer
    ],
)
def test_leaked_reasoning_before_the_answer_is_dropped(content: str) -> None:
    """Seen from deepseek:v4@flash on 2026-10-15: content `<reasoning></think><answer>`."""
    with respx.mock() as r:
        r.post(CHAT).mock(return_value=_ok(content))
        assert _call(_client()).output == {"answer": "present"}


@pytest.mark.parametrize(
    ("content", "finish", "match"),
    [("not json", "stop", "invalid JSON"), ('{"answer": "x"', "length", "token limit"),
     ("", "stop", "no content"), ('["a"]', "stop", "not an object")],
)  # fmt: skip
def test_unusable_output_fails_the_item(content: str, finish: str, match: str) -> None:
    with respx.mock() as r:
        r.post(CHAT).mock(return_value=_ok(content, finish))
        with pytest.raises(LLMError, match=match):
            _call(_client())


def test_profile_uses_two_different_models_for_dual_checks() -> None:
    cfg = load_llm_config(CONFIG_DIR, "runware")
    assert cfg.extractor.model == FLASH
    assert cfg.tiebreaker.model == "anthropic:claude@haiku-5.5"
    assert cfg.extractor.model != cfg.tiebreaker.model
    assert {cfg.extractor.model, cfg.tiebreaker.model, cfg.synthesis.model} <= set(cfg.pricing)


@pytest.mark.parametrize(
    "fixture", ["deepseek_v4_flash", "anthropic_claude_haiku-5.5", "anthropic_claude_sonnet-5.5"]
)
def test_recorded_responses_parse_and_validate(fixture: str) -> None:
    """Responses recorded by scripts/runware_smoke.py --record (2026-10-15)."""
    from tests.conftest import FIXTURES
    from uw_pipeline.enrich.news_relevance import Verdicts

    body = json.loads((FIXTURES / "runware" / f"{fixture}.json").read_text())
    with respx.mock() as r:
        r.post(CHAT).mock(return_value=httpx.Response(200, json=body))
        res = _call(_client())
    verdicts = Verdicts.model_validate(res.output)
    assert {a.id: a.verdict for a in verdicts.articles} == {"N1": "SUBJECT", "N2": "PASSING"}
    assert res.input_tokens > 0 and res.output_tokens > 0
