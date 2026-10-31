"""Industry group chosen by the LLM for a company whose SIC code maps to no group (Phase 17g,
D-059): the list of groups the models see, the schema, the checks, and which stored choice may
be used."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, and_, select

from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import IndustryConfig
from uw_pipeline.enrich.grounding import is_grounded
from uw_pipeline.enrich.schemas import IndustryChoice, strict_json_schema
from uw_pipeline.schema import industry_choice, review_decision

PROMPT_VERSION = "industry_choice_v1"
REVIEW_TYPE = "INDUSTRY"
CORE_SETS = frozenset({"COMMERCIAL_CORE", "FINANCIAL_CORE", "REPORTING_QUALITY"})


def choices(industry: IndustryConfig) -> list[str]:
    """The group codes a model may choose: every group but the default one."""
    return [code for code in industry.groups if code != industry.default_group]


def catalog(industry: IndustryConfig, registry: Registry) -> str:
    """One line per group: code, label and the metrics its own sets add to the core."""
    names = {m.code: m.display_name for m in registry.metrics}
    common = CORE_SETS | set(industry.common_metric_sets)
    lines = []
    for code in choices(industry):
        g = industry.groups[code]
        added = dict.fromkeys(names.get(c, c) for s in g.metric_sets if s not in common
                              for c in registry.metric_sets.get(s, [])
                              if c not in g.exclude_metrics)  # fmt: skip
        kind = "financial company" if g.is_financial else "operating company"
        extra = "; ".join(added) if added else "the common metrics only"
        lines.append(f"{code}: {g.label} ({kind}). Adds: {extra}.")
    return "\n".join(lines)


def schema(industry: IndustryConfig) -> dict[str, Any]:
    """The answer's JSON schema, the group restricted to the configured codes."""
    out = strict_json_schema(IndustryChoice)
    out["properties"]["group"]["enum"] = choices(industry)
    return out


def checked(choice: IndustryChoice, industry: IndustryConfig, source: str) -> str | None:
    """The chosen code when it is a configured group and its quote is in the business section;
    otherwise None."""
    quote = choice.quote.strip().strip("'\",;").strip()
    if choice.group not in choices(industry) or not quote or not is_grounded(quote, source):
        return None
    return choice.group


def review_key(company_id: int) -> str:
    return str(company_id)


def usable_choice(conn: Connection, company_id: int) -> str | None:
    """The stored group for the company, unless a reviewer rejected it."""
    rejected = select(review_decision.c.item_key).where(and_(
        review_decision.c.item_type == REVIEW_TYPE,
        review_decision.c.item_key == review_key(company_id),
        review_decision.c.decision == "REJECTED"))  # fmt: skip
    if conn.execute(rejected).first() is not None:
        return None
    row = conn.execute(select(industry_choice.c.group_code)
                       .where(industry_choice.c.company_id == company_id)).first()  # fmt: skip
    return row[0] if row else None
