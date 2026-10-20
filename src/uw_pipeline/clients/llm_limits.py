"""Requests- and tokens-per-minute limits per model, shared by parallel workers (Phase 15i, D-050).

Azure OpenAI (SecureGPT) counts a request against the deployment's tokens-per-minute quota at
its estimated input tokens plus `max_tokens` when it arrives (other providers estimate output
the same way at first). So a request reserves that much here before it is sent; once the
answer is in, the reservation is replaced by the tokens actually used. Each model has two
token buckets (requests and tokens) that refill continuously at limit/60 per second; a request
that does not fit waits. A throttling answer (HTTP 429) pauses the model's buckets for the time
the provider asks, so all workers back off together instead of each retrying on its own.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from uw_pipeline.clients.llm import LLMClient, LLMResult

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4  # rough English average; the reservation errs on the high side anyway


class ModelLimits(BaseModel):
    rpm: int | None = None  # requests per minute
    tpm: int | None = None  # tokens per minute (input + max_tokens reserved per request)


def estimate_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + 1


class _Bucket:
    """Refills `per_minute` units per minute up to `per_minute`; not locked on its own."""

    def __init__(self, per_minute: int, now: float) -> None:
        self.capacity = float(per_minute)
        self.level = float(per_minute)
        self.rate = per_minute / 60.0
        self.at = now

    def refill(self, now: float) -> None:
        self.level = min(self.capacity, self.level + (now - self.at) * self.rate)
        self.at = now

    def wait_for(self, amount: float) -> float:
        """Seconds until `amount` fits (a request above capacity waits for a full bucket)."""
        need = min(amount, self.capacity)
        return 0.0 if self.level >= need else (need - self.level) / self.rate


class _ModelGate:
    def __init__(self, limits: ModelLimits, now: float) -> None:
        self.requests = _Bucket(limits.rpm, now) if limits.rpm else None
        self.tokens = _Bucket(limits.tpm, now) if limits.tpm else None
        self.paused_until = 0.0
        self.lock = threading.Lock()


class LimitedClient:
    """An LLMClient that stays under per-model RPM / TPM limits across threads. Models without
    limits pass straight through. Other attributes (`budget`, `task_max_tokens`) live here."""

    def __init__(
        self,
        inner: LLMClient,
        limits: dict[str, ModelLimits],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self._clock, self._sleep = clock, sleep
        now = clock()
        self._gates = {m: _ModelGate(lim, now) for m, lim in limits.items() if lim.rpm or lim.tpm}
        self.waited = 0.0  # seconds spent waiting for capacity (run statistics)
        # The inner client calls this when the provider throttles (status 429).
        if hasattr(inner, "on_throttle"):
            inner.on_throttle = self.pause

    def reserve(self, model: str, tokens: int) -> None:
        gate = self._gates.get(model)
        if gate is None:
            return
        while True:
            with gate.lock:
                now = self._clock()
                wait = max(0.0, gate.paused_until - now)
                for bucket, amount in ((gate.requests, 1), (gate.tokens, tokens)):
                    if bucket is not None:
                        bucket.refill(now)
                        wait = max(wait, bucket.wait_for(amount))
                if wait <= 0:
                    if gate.requests is not None:
                        gate.requests.level -= 1
                    if gate.tokens is not None:
                        gate.tokens.level -= min(tokens, gate.tokens.capacity)
                    return
            self.waited += wait
            self._sleep(wait)

    def settle(self, model: str, reserved: int, used: int) -> None:
        """Give back what a request reserved but did not use (or take the overrun)."""
        gate = self._gates.get(model)
        if gate is None or gate.tokens is None:
            return
        with gate.lock:
            gate.tokens.refill(self._clock())
            reserved = min(reserved, int(gate.tokens.capacity))
            gate.tokens.level = min(gate.tokens.capacity, gate.tokens.level + reserved - used)

    def pause(self, model: str, seconds: float) -> None:
        gate = self._gates.get(model)
        if gate is None:
            return
        with gate.lock:
            gate.paused_until = max(gate.paused_until, self._clock() + seconds)
        log.info("%s throttled by the provider: all workers wait %.0f s", model, seconds)

    def complete_json(
        self, *, model: str, effort: str, max_tokens: int, system: str, user: str,
        schema: dict[str, Any],
    ) -> LLMResult:  # fmt: skip
        reserved = estimate_tokens(system, user) + max_tokens
        self.reserve(model, reserved)
        result = self.inner.complete_json(model=model, effort=effort, max_tokens=max_tokens,
                                          system=system, user=user, schema=schema)  # fmt: skip
        used = (result.input_tokens or 0) + (result.output_tokens or 0)
        self.settle(model, reserved, used or reserved)
        return result
