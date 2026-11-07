"""uw_metric_chat: the metric chat of Professional 360 (Phase 19, D-088, D-090, D-091).

Copied as a folder into the BFF (Python FastAPI); never edited there. It needs only pydantic
and httpx: no database, no pipeline code. The BFF reads the metric's context
(udf_GetMetricChatContext) and the active prompt (udf_GetActiveChatPrompt), then calls

    reply = await answer_question(client, prompt, context, history, question)

which builds the messages, asks SecureGPT, checks every number in the answer against the
context, and returns what to show and what to log (UserMetricChat).
"""

from __future__ import annotations

from .chat import CANNOT_CONFIRM, OUT_OF_SCOPE, answer_question
from .grounding import Check, check_answer
from .models import (
    ChatAnswer,
    ChatContext,
    ChatPromptRow,
    ChatReply,
    Citation,
    HistoryTurn,
)
from .prompting import ANSWER_SCHEMA, build_messages
from .securegpt import AsyncSecureGPT, GatewayError, SecureGPTSettings
from .version import (
    SUPPORTED_CONTEXT_VERSIONS,
    VERSION,
    IncompatibleError,
    check_context,
    check_prompt,
)

__version__ = VERSION
__all__ = [
    "ANSWER_SCHEMA",
    "CANNOT_CONFIRM",
    "OUT_OF_SCOPE",
    "SUPPORTED_CONTEXT_VERSIONS",
    "VERSION",
    "AsyncSecureGPT",
    "ChatAnswer",
    "ChatContext",
    "ChatPromptRow",
    "ChatReply",
    "Check",
    "Citation",
    "GatewayError",
    "HistoryTurn",
    "IncompatibleError",
    "SecureGPTSettings",
    "answer_question",
    "build_messages",
    "check_answer",
    "check_context",
    "check_prompt",
]
