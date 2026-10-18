"""Phase 15i (D-050): per-model request and token limits shared by parallel workers, and
per-task max_tokens."""

from datetime import UTC, datetime

from sqlalchemy import create_engine

from uw_pipeline.clients.llm import LLMResult
from uw_pipeline.clients.llm_limits import LimitedClient, ModelLimits, estimate_tokens
from uw_pipeline.enrich.llm import ModelSpec, cached_call
from uw_pipeline.schema import metadata


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(round(s, 3))
        self.t += s


class Echo:
    """A client that answers at once, using `used` tokens, and records max_tokens."""

    def __init__(self, used: int = 100) -> None:
        self.used, self.max_tokens, self.on_throttle = used, [], None

    def complete_json(self, *, model, effort, max_tokens, system, user, schema):
        self.max_tokens.append(max_tokens)
        return LLMResult({"ok": True}, model, self.used - 10, 10)


def call(client, model="m", max_tokens=400, user="x" * 396):
    return client.complete_json(model=model, effort="low", max_tokens=max_tokens, system="",
                                user=user, schema={})  # fmt: skip


def test_requests_per_minute() -> None:
    clock = Clock()
    client = LimitedClient(Echo(), {"m": ModelLimits(rpm=2)}, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        call(client)
    assert clock.slept == [30.0]  # the third request waits for one request's worth of refill
    call(client, model="other")  # a model without limits never waits
    assert clock.slept == [30.0]


def test_tokens_reserved_at_max_tokens_then_settled() -> None:
    clock = Clock()
    inner = Echo(used=100)
    client = LimitedClient(inner, {"m": ModelLimits(tpm=1000)}, clock=clock, sleep=clock.sleep)
    assert estimate_tokens("x" * 396) == 100
    call(client)  # reserves 100 + 400 = 500, uses 100: 900 left
    call(client)  # 800 left
    assert clock.slept == []
    call(client, max_tokens=900)  # needs 1000: waits for 200 tokens at 1000/60 per second
    assert clock.slept == [12.0]


def test_throttling_pauses_every_worker() -> None:
    clock = Clock()
    inner = Echo()
    client = LimitedClient(inner, {"m": ModelLimits(rpm=600)}, clock=clock, sleep=clock.sleep)
    inner.on_throttle("m", 20.0)  # the client saw a 429 asking for 20 seconds
    call(client)
    assert clock.slept == [20.0]


def test_task_max_tokens_caps_the_request() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    inner = Echo()
    inner.task_max_tokens = {"board": 4000}
    spec = ModelSpec(model="m", effort="low", max_tokens=16000)
    now = datetime(2026, 10, 17, tzinfo=UTC)
    with engine.begin() as conn:
        for task in ("board", "insights"):
            cached_call(conn, inner, spec, task=task, prompt_version="v1", system="s",
                        user=task, schema={}, now=now)  # fmt: skip
    assert inner.max_tokens == [4000, 16000]
