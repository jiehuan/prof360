"""One chat turn: messages -> SecureGPT -> grounding check -> what to show and log."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from .grounding import check_answer
from .models import (
    ChatAnswer,
    ChatContext,
    ChatPromptRow,
    ChatReply,
    Citation,
    HistoryTurn,
)
from .prompting import ANSWER_SCHEMA, build_messages
from .securegpt import DEFAULT_MODEL, Completion, GatewayError
from .version import check_context, check_prompt

log = logging.getLogger(__name__)

CANNOT_CONFIRM = (
    "I can't confirm that from the data behind this metric. Please check the Data Sources "
    "in this view, or report the value if it looks wrong."
)
OUT_OF_SCOPE = "I can only answer questions about this metric for this company."
UNAVAILABLE = "The assistant is not available right now. Please try again later."


class ChatModel(Protocol):
    """AsyncSecureGPT, or a stand-in in tests and evaluations."""

    async def complete_json(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        schema: Any,
        max_tokens: int = ...,
    ) -> Completion: ...


async def answer_question(
    client: ChatModel,
    prompt: ChatPromptRow,
    context: ChatContext | str | dict[str, Any],
    history: Sequence[HistoryTurn],
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1500,
) -> ChatReply:
    """Answer `question` about the context's metric. Never raises for the model's or the
    gateway's failures: the reply then has grounding = "error" and a fixed message, and
    `grounding_detail` says what happened. Raises IncompatibleError when this package cannot
    read the prompt or the context (replace the folder)."""
    ctx = context if isinstance(context, ChatContext) else ChatContext.parse(context)
    check_prompt(prompt)
    check_context(ctx.version)
    base: dict[str, Any] = {"model": model, "prompt_version": prompt.prompt_version,
                            "context_version": ctx.version}  # fmt: skip
    messages = build_messages(prompt, ctx, history, question)
    try:
        done = await client.complete_json(model=model, messages=messages,
                                          schema=prompt.output_schema or ANSWER_SCHEMA,
                                          max_tokens=max_tokens)  # fmt: skip
    except GatewayError as exc:
        log.warning("metric chat: %s error: %s", exc.kind, exc)
        return ChatReply(answer=UNAVAILABLE, grounding="error",
                         grounding_detail=f"{exc.kind}: {exc}", **base)  # fmt: skip
    base |= {"model": done.model, "raw_answer": done.raw, "input_tokens": done.input_tokens,
             "output_tokens": done.output_tokens, "latency_ms": done.latency_ms}  # fmt: skip
    try:
        answer = ChatAnswer.model_validate(done.output)
    except ValidationError as exc:
        return ChatReply(answer=CANNOT_CONFIRM, grounding="error",
                         grounding_detail=f"answer does not match the schema: {exc}"[:1000],
                         **base)  # fmt: skip
    check = check_answer(answer, ctx, question)
    if not check.ok:
        return ChatReply(answer=CANNOT_CONFIRM, grounding="rejected",
                         grounding_detail=check.detail, in_scope=answer.in_scope,
                         **base)  # fmt: skip
    points = ctx.points()
    citations = [
        Citation(id=p.id, label=p.label, value=p.value, unit=p.unit, status=p.status,
                 period=p.period)
        for c in dict.fromkeys([*answer.citations, *check.added]) if (p := points[c])
    ]  # fmt: skip
    text = answer.answer.strip() or (OUT_OF_SCOPE if not answer.in_scope else CANNOT_CONFIRM)
    return ChatReply(
        answer=text, citations=citations, in_scope=answer.in_scope,
        possible_issue=answer.possible_issue,
        issue_summary=(answer.issue_summary or None) if answer.possible_issue else None,
        period=answer.period, grounding="ok", grounding_detail=check.detail or None, **base,
    )  # fmt: skip
