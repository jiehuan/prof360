"""Deterministic stand-in for the LLM client (unit tests never call the API)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uw_pipeline.clients.llm import LLMError, LLMResult


class FakeLLM:
    def __init__(self, answer: Callable[[str, str], dict[str, Any] | Exception]) -> None:
        """`answer(model, user_message)` returns the output dict, or an exception to raise."""
        self.answer = answer
        self.calls: list[str] = []

    def complete_json(self, *, model: str, effort: str, max_tokens: int, system: str,
                      user: str, schema: dict[str, Any]) -> LLMResult:  # fmt: skip
        self.calls.append(model)
        out = self.answer(model, user)
        if isinstance(out, Exception):
            raise out
        return LLMResult(output=out, model=model, input_tokens=100, output_tokens=20)


def signals(leadership=(), findings=(), summary="x") -> dict[str, Any]:
    return {
        "leadership_changes": [
            {"role": r, "change": c, "person": p, "effective_date": None, "evidence": e}
            for r, c, p, e in leadership
        ],
        "findings": [{"topic": t, "evidence": e} for t, e in findings],
        "summary": summary,
    }


NOTHING = signals()
REFUSED = LLMError("declined")
