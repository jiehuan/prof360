"""The answer's JSON schema and the messages sent to the model."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import ChatContext, ChatPromptRow, HistoryTurn

MAX_QUESTION_CHARS = 1000
MAX_HISTORY_TURNS = 6

# Structured output (strict): every property required, nulls typed explicitly.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "citations", "in_scope", "possible_issue", "issue_summary", "period"],
    "properties": {
        "answer": {"type": "string", "description": "Plain English, at most 150 words."},
        "citations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of every data point whose number the answer uses or computes "
            "from, e.g. dp:FY2025:interest_expense.",
        },
        "in_scope": {
            "type": "boolean",
            "description": "False when the question is not about this company's metric.",
        },
        "possible_issue": {
            "type": "boolean",
            "description": "True when the data suggests the value may be wrong or misleading.",
        },
        "issue_summary": {
            "type": ["string", "null"],
            "description": "One sentence on the possible issue; null when there is none.",
        },
        "period": {
            "type": ["string", "null"],
            "description": "The period the answer is mainly about (FY2025, 2026Q3), or null.",
        },
    },
}


def build_messages(
    prompt: ChatPromptRow,
    ctx: ChatContext,
    history: Sequence[HistoryTurn],
    question: str,
) -> list[dict[str, str]]:
    """OpenAI chat messages: the prompt with the context appended (the context is the
    pipeline's own data), the last turns of the conversation, then the question."""
    system = (
        f"{prompt.system_text.rstrip()}\n\n## Context\n\n"
        "The JSON below is the only data you may use.\n\n"
        f"```json\n{ctx.as_json()}\n```"
    )
    messages = [{"role": "system", "content": system}]
    for turn in list(history)[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn.question[:MAX_QUESTION_CHARS]})
        messages.append({"role": "assistant", "content": turn.answer})
    messages.append({"role": "user", "content": question.strip()[:MAX_QUESTION_CHARS]})
    return messages
