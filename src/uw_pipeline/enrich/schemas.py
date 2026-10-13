"""Pydantic schemas for LLM outputs. Every LLM output is validated against these."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FindingTopic = Literal[
    "going_concern",
    "material_weakness",
    "controls_not_effective",
    "restatement",
    "listing_adverse",
    "listing_voluntary_transfer",
]


class LeadershipChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["CEO", "CFO"]
    change: Literal["departure", "appointment"]
    person: str | None = Field(description="Name of the person, if stated")
    effective_date: str | None = Field(description="Effective date as written, if stated")
    evidence: str = Field(description="Verbatim quote from the excerpt, at most 300 characters")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: FindingTopic
    evidence: str = Field(description="Verbatim quote from the excerpt, at most 300 characters")


class SectionSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leadership_changes: list[LeadershipChange]
    findings: list[Finding]
    summary: str


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for structured outputs: every property required, no extra properties."""

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    schema: dict[str, Any] = fix(model.model_json_schema())
    return schema


TopicAnswer = Literal[
    "departure", "appointment", "succession", "adverse", "voluntary_transfer", "present", "none"
]  # fmt: skip


class Ruling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    answer: TopicAnswer
    evidence: str = Field(
        description="Verbatim quote from the excerpt supporting the answer; empty if none"
    )
    reason: str = Field(description="One sentence")


class TieBreak(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rulings: list[Ruling]
