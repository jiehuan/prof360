"""Pydantic schemas for LLM outputs. Every LLM output is validated against these."""

from __future__ import annotations

from typing import Annotated, Any, Literal

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
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("title", "default"):
                    continue  # schema annotations, not fields
                if k in ("properties", "$defs"):
                    out[k] = {name: fix(sub) for name, sub in v.items()}  # keep every field name
                else:
                    out[k] = fix(v)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
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


# -- Phase 5a: numbers from text -----------------------------------------------------------------

Scale = Literal["units", "thousands", "millions", "billions"]


class ExtractedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_code: str
    value: str = Field(description="The number exactly as written in the excerpt, e.g. 69.3")
    unit: Literal["percent", "basis_points", "usd"]
    scale: Scale = Field(description="From the table header or text, e.g. 'in millions'")
    period_start: str | None = Field(
        description="First day of the period, YYYY-MM-DD; null for point_in_time"
    )
    period_end: str = Field(description="Last day of the period or the as-of date, YYYY-MM-DD")
    period: Literal["quarter", "year_to_date", "year", "point_in_time"]
    evidence: str = Field(description="Verbatim quote (<= 300 chars) that contains the value")


class TextValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: list[ExtractedValue]


class LoanSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment: str = Field(description="Loan class as named in the table, e.g. Auto and other")
    parent_segment: str = Field(description="The firm's top-level segment it belongs to")
    outstanding: str = Field(description="Number exactly as written")
    nonaccrual: str | None = Field(description="Number exactly as written, if shown")
    scale: Scale
    period_end: str = Field(description="YYYY-MM-DD")
    evidence: str = Field(description="Verbatim table row (<= 300 chars) with the numbers")


class LoanBook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[LoanSegment]


# -- Phase 5b: insights ----------------------------------------------------------------------------


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="Id of the input item quoted, e.g. E3, M12, S2, T1")
    quote: str = Field(description="Verbatim text from that item, at most 300 characters")


class ActivitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: str
    summary: str = Field(description="Two sentences at most, facts from the article only")
    uw_relevance: str = Field(description="One sentence on why it matters to a D&O underwriter")
    sentiment: Literal["Positive", "Negative", "Neutral", "Mixed"]


class ActivitySummaries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activities: list[ActivitySummary]


class BehavioralInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subcategory: Literal[
        "Tone Shift", "Strategic Focus Shift", "Emerging Risk Language", "Wording Change"
    ]
    title: str = Field(description="At most 8 words")
    description: str
    uw_relevance: str
    evidence: list[Evidence]


class BehavioralInsights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insights: list[BehavioralInsight]


StrategicCategory = Literal["Governance & Leadership", "Risk & Legal", "Event Signals"]


class StrategicInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: StrategicCategory
    subcategory: str
    title: str = Field(description="At most 8 words")
    description: str
    uw_relevance: str
    event_date: str | None = Field(description="YYYY-MM-DD if the insight is about one event")
    evidence: list[Evidence]


class NoSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: StrategicCategory
    rationale: str


class StrategicInsights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insights: list[StrategicInsight]
    no_signal: list[NoSignal] = Field(description="Categories reviewed with nothing to report")


class KeyInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Warning", "Negative", "Positive", "Info"]
    title: str = Field(description="At most 8 words")
    summary: str
    uw_relevance: str
    supporting_evidence: str = Field(description="The facts behind the insight, briefly")
    related_metric_codes: list[str]
    evidence: list[Evidence]


class KeyInsights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insights: list[KeyInsight]


class FollowUpQuestion(BaseModel):
    """followups_v2 (Phase 8): the fields of the Follow-Up Question modal."""

    model_config = ConfigDict(extra="forbid")

    category: Literal["Financial", "Operational", "Market", "Strategy", "Governance"]
    question: str
    summary: str = Field(description="The background facts behind the question, 1-2 sentences")
    following_risk: str = Field(description="The risk area it probes, at most 5 words")
    uw_relevance: str = Field(description="Why the answer matters for D&O risk, one sentence")
    system_priority: Literal["high", "medium", "low"]
    related_insight_ids: list[str] = Field(description="Ids of insights it follows from, e.g. K2")
    related_metric_ids: list[str] = Field(description="Ids of metric rows it concerns, e.g. M4")


class FollowUpQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[FollowUpQuestion]


# -- Phase 8: metric detail ---------------------------------------------------------------------


class MetricRelevance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_code: str = Field(description="Code from the metric list, e.g. debt_to_equity")
    uw_relevance: str = Field(description="One or two sentences on the D&O angle")
    related_insight_ids: list[str] = Field(description="K/R/B ids about this metric")
    related_question_ids: list[str] = Field(description="Q ids about this metric")


class MetricRelevances(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: list[MetricRelevance]


# -- Phase 9.4: risk scores -----------------------------------------------------------------------

# Ranges are stated in the description and clamped by the caller (structured outputs do not
# enforce numeric bounds, and one stray value must not invalidate a whole batch).
Score = Annotated[float, Field(description="0.00 (none) to 1.00 (severe)")]


class NewsScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    litigation: Score
    regulatory: Score
    financial_distress: Score
    governance: Score
    operational: Score
    overall: Score
    rationale: str = Field(description="One sentence naming the facts behind the scores")


class NewsScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    articles: list[NewsScore]


class RiskFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "litigation", "regulatory", "financial_distress", "governance", "operational",
        "guidance", "accounting",
    ]  # fmt: skip
    quote: str = Field(description="Verbatim sentence from the chunk, at most 300 characters")


class ChunkScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk: str = Field(description="Chunk id, e.g. C3")
    tone: float = Field(description="-1.00 (very negative) to 1.00 (very positive)")
    confidence: Score
    hedging: Score
    uncertainty: Score
    forward_looking: Score
    liability_cues: Score
    flags: list[RiskFlag]


class ChunkScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[ChunkScore]
