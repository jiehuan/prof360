"""Is an article about the company? LLM verdict per stored article (Phase 9, decisions D-037).

Rules (normalize/news.about_company) drop the obvious cases first; the model judges the rest,
in batches of headlines with summaries. Calls are cached by input, and each article's verdict
is stored, so an article is judged once.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.enrich.llm import PROMPTS_DIR, ModelSpec, cached_call
from uw_pipeline.enrich.schemas import strict_json_schema

Verdict = Literal["SUBJECT", "PASSING", "ANALYST", "HOLDING", "MARKET"]


class ArticleVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    verdict: Verdict


class Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    articles: list[ArticleVerdict]


SCHEMA = strict_json_schema(Verdicts)


def judge(
    items: Sequence[tuple[int, str, str]],  # (news_id, title, summary)
    *,
    company: str,
    client: LLMClient,
    spec: ModelSpec,
    prompt_version: str,
    batch_size: int,
    conn: Connection,
    now: Callable[[], datetime],
) -> dict[int, str]:
    """news_id -> verdict; ids without a usable answer get UNCLASSIFIED."""
    system = (PROMPTS_DIR / f"{prompt_version}.md").read_text(encoding="utf-8")
    out: dict[int, str] = {}
    ordered = sorted(items)
    for start in range(0, len(ordered), batch_size):
        batch = ordered[start : start + batch_size]
        lines = [f"N{i} | {t} | {(s or '')[:300]}" for i, t, s in batch]
        raw = cached_call(
            conn, client, spec,
            task="news_relevance", prompt_version=prompt_version, system=system,
            user=f"Company: {company}\n\nid | headline | summary\n" + "\n".join(lines),
            schema=SCHEMA, now=now(),
        )  # fmt: skip
        got: dict[int, str] = {}
        if raw is not None:
            try:
                for a in Verdicts.model_validate(raw).articles:
                    key = a.id.strip().lstrip("N")
                    if key.isdigit():
                        got[int(key)] = a.verdict
            except ValidationError:
                pass
        for i, _, _ in batch:
            out[i] = got.get(i, "UNCLASSIFIED")
    return out
