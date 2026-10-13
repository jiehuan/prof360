"""Tier-3 news de-duplication: group headlines about the same event with the LLM."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.enrich.llm import PROMPTS_DIR, ModelSpec, cached_call
from uw_pipeline.enrich.schemas import strict_json_schema
from uw_pipeline.normalize.news import Doc


class EventGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str]
    event: str


class EventGroups(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[EventGroup]


SCHEMA = strict_json_schema(EventGroups)


def event_pairs(
    docs: Sequence[Doc],
    sources: dict[str, str],
    *,
    company: str,
    client: LLMClient,
    spec: ModelSpec,
    prompt_version: str,
    batch_size: int,
    window: timedelta,
    conn: Connection,
    now: Callable[[], datetime],
) -> tuple[list[tuple[str, str]], int]:
    """Key pairs judged to be the same event, and the number of ids the model invented.

    Docs are sent in time order, in overlapping batches so that an event at a batch edge is
    still seen together. Only pairs published within `window` of each other are kept."""
    system = (PROMPTS_DIR / f"{prompt_version}.md").read_text(encoding="utf-8")
    ordered = sorted(docs, key=lambda d: (d.published_at, d.key))
    by_key = {d.key: d for d in ordered}
    step = max(1, batch_size - batch_size // 4)
    pairs: list[tuple[str, str]] = []
    invented = 0
    for start in range(0, max(1, len(ordered) - batch_size // 4), step):
        batch = ordered[start : start + batch_size]
        if len(batch) < 2:
            break
        lines = [
            f"{d.key} | {d.published_at:%Y-%m-%d} | {sources.get(d.key, '')} | {d.title}"
            for d in batch
        ]
        raw = cached_call(
            conn, client, spec,
            task="news_events", prompt_version=prompt_version, system=system,
            user=f"Company: {company}\n\nid | date | source | headline\n" + "\n".join(lines),
            schema=SCHEMA, now=now(),
        )  # fmt: skip
        if raw is None:
            continue
        try:
            parsed = EventGroups.model_validate(raw)
        except ValidationError:
            continue
        in_batch = {d.key for d in batch}
        for group in parsed.groups:
            ids = [i for i in group.ids if i in in_batch]
            invented += len(group.ids) - len(ids)
            members = sorted((by_key[i] for i in set(ids)), key=lambda d: d.published_at)
            for a, b in itertools.pairwise(members):
                if b.published_at - a.published_at <= window:
                    pairs.append((a.key, b.key))
    return pairs, invented
