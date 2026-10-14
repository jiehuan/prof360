"""Company-level LLM tasks with grounding (Phase 5b, decisions D-033).

Every generated item must cite input items; each quote must be found in the item it cites,
and every number in the text must match a metric value in the package or appear in a cited
item (the quote or the cited item's text, decisions D-033). Items that fail are dropped and
counted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.enrich.grounding import is_grounded, ungrounded_numbers
from uw_pipeline.enrich.llm import PROMPTS_DIR, ModelSpec, cached_call
from uw_pipeline.enrich.package import Package, Source
from uw_pipeline.enrich.schemas import Evidence, strict_json_schema

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class GroundingStats:
    generated: int = 0
    kept: int = 0
    no_evidence: int = 0
    bad_quotes: int = 0
    bad_numbers: int = 0
    examples: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if len(self.examples) < 5:
            self.examples.append(msg)


@dataclass(frozen=True)
class Cited:
    source: Source
    quote: str


def check_evidence(
    evidence: Sequence[Evidence], pkg: Package, stats: GroundingStats
) -> list[Cited]:
    """Evidence whose source exists and whose quote is found in that source."""
    good = []
    for e in evidence:
        src = pkg.sources.get(e.source_id.strip())
        # Metric rows are short; quoting just the value (e.g. "$16.91B") is fine there.
        min_chars = 4 if src is not None and src.kind == "METRIC" else 8
        if src is None or not is_grounded(e.quote, src.text, min_chars=min_chars):
            stats.bad_quotes += 1
            continue
        good.append(Cited(src, e.quote))
    return good


def numbers_ok(
    texts: Sequence[str], cited: Sequence[Cited], pkg: Package, stats: GroundingStats, what: str
) -> bool:
    values = [v for s in pkg.of("METRIC") for v in s.values]
    quotes = [c.quote for c in cited] + [c.source.text for c in cited if c.source.kind != "METRIC"]
    missing = ungrounded_numbers(" ".join(texts), quotes, values)
    if missing:
        stats.bad_numbers += 1
        stats.note(f"{what}: unverified numbers {missing}")
        return False
    return True


class TaskRunner:
    def __init__(
        self,
        client: LLMClient,
        conn: Connection,
        now: Callable[[], datetime],
        common_prompt: str,
    ) -> None:
        self.client, self.conn, self.now, self.common = client, conn, now, common_prompt

    def run(
        self,
        spec: ModelSpec,
        task: str,
        prompt_version: str,
        user: str,
        model: type[T],
        *,
        with_common: bool = True,
    ) -> T | None:
        task_prompt = (PROMPTS_DIR / f"{prompt_version}.md").read_text(encoding="utf-8")
        system = f"{self.common}\n\n{task_prompt}" if with_common else task_prompt
        raw = cached_call(
            self.conn, self.client, spec, task=task, prompt_version=prompt_version,
            system=system, user=user, schema=strict_json_schema(model), now=self.now(),
            validate=model.model_validate,
        )  # fmt: skip
        if raw is None:
            return None
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            log.warning("%s: output failed schema validation: %s", task, exc)
            return None


def ground_items(
    items: Sequence[Any],
    pkg: Package,
    text_fields: Sequence[str],
    what: str,
) -> tuple[list[tuple[Any, list[Cited]]], GroundingStats]:
    """Keep items with at least one verified quote and only verified numbers."""
    stats = GroundingStats(generated=len(items))
    kept = []
    for item in items:
        cited = check_evidence(item.evidence, pkg, stats)
        if not cited:
            stats.no_evidence += 1
            stats.note(f"{what} '{getattr(item, 'title', '')}': no verifiable evidence")
            continue
        texts = [str(getattr(item, f) or "") for f in text_fields]
        if not numbers_ok(texts, cited, pkg, stats, f"{what} '{getattr(item, 'title', '')}'"):
            continue
        kept.append((item, cited))
    stats.kept = len(kept)
    return kept, stats
