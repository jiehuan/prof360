"""Pydantic models: the context the BFF reads, the model's answer, and the reply to show and log.
FastAPI can use them as request and response types."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DataPoint(BaseModel):
    """A number the chat may cite: `dp:<period>:<name>` (dp:FY2025:interest_expense)."""

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    value: float | None
    unit: str
    status: str = "OK"

    @property
    def period(self) -> str:
        """FY2025 | 2026Q3, from the id."""
        parts = self.id.split(":")
        return parts[1] if len(parts) > 2 else ""


class ChatContext(BaseModel):
    """MetricChatContext.ContextJson (udf_GetMetricChatContext's `context`). Only the fields
    the package uses are typed; the rest is kept and sent to the model as it is."""

    model_config = ConfigDict(extra="allow")

    version: int
    company: dict[str, Any]
    metric: dict[str, Any]
    periods: list[dict[str, Any]] = Field(default_factory=list)
    data_points: list[DataPoint] = Field(default_factory=list)

    @classmethod
    def parse(cls, raw: str | bytes | dict[str, Any]) -> ChatContext:
        data = json.loads(raw) if isinstance(raw, str | bytes) else raw
        return cls.model_validate(data)

    def points(self) -> dict[str, DataPoint]:
        return {p.id: p for p in self.data_points}

    def as_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          separators=(",", ":"))  # fmt: skip


class ChatPromptRow(BaseModel):
    """ChatPrompt, as udf_GetActiveChatPrompt returns it (camelCase) or by column name."""

    model_config = ConfigDict(populate_by_name=True)

    prompt_version: str = Field(alias="promptVersion")
    system_text: str = Field(alias="systemText")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    context_versions: str = Field(alias="contextVersions")
    min_package_version: str = Field(alias="minPackageVersion")

    @field_validator("output_schema", mode="before")
    @classmethod
    def _schema(cls, v: Any) -> Any:
        return json.loads(v) if isinstance(v, str | bytes) else v


class HistoryTurn(BaseModel):
    """An earlier turn of the same conversation: the question and the answer the user saw."""

    question: str
    answer: str


class ChatAnswer(BaseModel):
    """What the model returns (prompting.ANSWER_SCHEMA)."""

    answer: str
    citations: list[str]
    in_scope: bool
    possible_issue: bool
    issue_summary: str | None
    period: str | None


class Citation(BaseModel):
    """A cited data point, for the front end to show and to jump to its period."""

    id: str
    label: str
    value: float | None
    unit: str
    status: str
    period: str


Grounding = Literal["ok", "rejected", "error"]


class ChatReply(BaseModel):
    """The turn's result: `answer` and `citations` are what to show; the rest is logged."""

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    in_scope: bool = True
    possible_issue: bool = False
    issue_summary: str | None = None
    period: str | None = None
    grounding: Grounding
    grounding_detail: str | None = None
    raw_answer: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    context_version: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None

    def log_row(self) -> dict[str, Any]:
        """The UserMetricChat columns this turn fills (the BFF adds ConversationId, UserId,
        CompanyId, metric_id, Question and ContextUpdatedAt)."""
        return {
            "Answer": self.answer,
            "RawAnswer": self.raw_answer,
            "Citations": json.dumps([c.id for c in self.citations]),
            "PossibleIssue": self.possible_issue,
            "IssueSummary": self.issue_summary,
            "Grounding": self.grounding,
            "GroundingDetail": (self.grounding_detail or "")[:1000] or None,
            "Model": self.model,
            "PromptVersion": self.prompt_version,
            "ContextVersion": self.context_version,
            "InputTokens": self.input_tokens,
            "OutputTokens": self.output_tokens,
            "LatencyMs": self.latency_ms,
        }
