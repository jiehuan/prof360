"""Phase 9.4 `scores` stage (decisions D-037): risk scores with the extractor model.

- News: litigation / regulatory / financial distress / governance / operational and overall,
  0.00-1.00, with a rationale, per activity -> news_risk_score. The rationale's numbers must
  appear in the article text.
- Transcripts: each chunk scored for tone (-1..1), confidence, hedging, uncertainty,
  forward-looking and liability cues; the transcript value is the length-weighted mean of the
  chunks with a usable answer -> transcript_tone. Risk flags must quote the chunk verbatim
  -> transcript_flag.
Items are re-scored only when their text (or the prompt version) changes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine, delete, insert, select

from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.grounding import is_grounded, ungrounded_numbers
from uw_pipeline.enrich.llm import PROMPTS_DIR, LLMConfig, cached_call, short_transactions
from uw_pipeline.enrich.schemas import ChunkScores, NewsScores, strict_json_schema
from uw_pipeline.enrich.untrusted import data_block, one_line
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.schema import (
    activity,
    filing_section,
    news_risk_score,
    section_chunk,
    source_document,
    transcript_flag,
    transcript_tone,
)

log = logging.getLogger(__name__)

NEWS_DIMS = ("litigation", "regulatory", "financial_distress", "governance", "operational",
             "overall")  # fmt: skip
TONE_DIMS = ("confidence", "hedging", "uncertainty", "forward_looking", "liability_cues")


class NewsScoreConfig(BaseModel):
    prompt_version: str
    batch_size: int


class TranscriptScoreConfig(BaseModel):
    prompt_version: str
    chunks_per_call: int
    max_flags: int


class ScoresConfig(BaseModel):
    news: NewsScoreConfig
    transcript: TranscriptScoreConfig


def load_scores_config(config_dir: Path) -> ScoresConfig:
    with (config_dir / "scores.yaml").open(encoding="utf-8") as f:
        return ScoresConfig.model_validate(yaml.safe_load(f))


@dataclass
class Stats:
    news_scored: int = 0
    news_unchanged: int = 0
    news_dropped: int = 0  # rationales withheld (unverified numbers)
    transcripts_scored: int = 0
    transcripts_unchanged: int = 0
    chunks: int = 0
    chunks_attempted: int = 0
    flags: int = 0
    flags_dropped: int = 0


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return round(min(hi, max(lo, float(x))), 2)


def weighted_mean(
    scores: dict[int, Any], weights: dict[int, int], attr: str, lo: float = 0.0
) -> float | None:
    """Length-weighted mean of one score over the chunks, clamped to [lo, 1]."""
    total = sum(weights.values())
    if not total:
        return None
    return clamp(sum(clamp(getattr(scores[i], attr), lo) * w for i, w in weights.items()) / total,
                 lo)  # fmt: skip


def _call(conn: Connection, client: LLMClient, llm: LLMConfig, task: str, version: str,
          user: str, model: type[BaseModel], now: datetime) -> Any:  # fmt: skip
    system = (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    raw = cached_call(
        conn, client, llm.extractor, task=task, prompt_version=version, system=system,
        user=user, schema=strict_json_schema(model), now=now,
        validate=model.model_validate,
    )  # fmt: skip
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        log.warning("%s: output failed schema validation: %s", task, exc)
        return None


# -- news ----------------------------------------------------------------------------------------


def score_news(
    conn: Connection, client: LLMClient, llm: LLMConfig, cfg: NewsScoreConfig,
    company_id: int, company: str, run_id: int | None, now: datetime, stats: Stats,
) -> None:  # fmt: skip
    have = dict(conn.execute(
        select(news_risk_score.c.activity_id, news_risk_score.c.input_hash)
        .where(news_risk_score.c.company_id == company_id)
    ).all())  # fmt: skip
    todo: list[tuple[int, str]] = []
    for a in conn.execute(
        select(activity.c.activity_id, activity.c.title, activity.c.av_summary,
               activity.c.published_at)
        .where(activity.c.company_id == company_id)
        .order_by(activity.c.published_at)
    ):  # fmt: skip
        text = f"{a.published_at:%Y-%m-%d} | {one_line(a.title)} | {one_line(a.av_summary or '')}"
        if have.get(a.activity_id) == _hash(cfg.prompt_version, text):
            stats.news_unchanged += 1
        else:
            todo.append((a.activity_id, text))
    for start in range(0, len(todo), cfg.batch_size):
        batch = dict(todo[start : start + cfg.batch_size])
        user = f"Company: {company}\n\nid | date | headline | summary\n" + "\n".join(
            f"A{aid} | {text}" for aid, text in batch.items()
        )
        out = _call(conn, client, llm, "news_risk", cfg.prompt_version, user, NewsScores, now)
        if out is None:
            continue
        rows = []
        for s in out.articles:
            key = s.id.strip().lstrip("A")
            aid = int(key) if key.isdigit() else -1
            if aid not in batch:
                continue
            rationale = s.rationale
            if ungrounded_numbers(rationale, [batch[aid]], []):
                # A number the article does not contain: the rationale is not shown, the scores
                # (judgments, no figures) are kept so the article is not re-sent every run.
                stats.news_dropped += 1
                rationale = ""
            rows.append({
                "activity_id": aid, "company_id": company_id,
                **{d: clamp(getattr(s, d)) for d in NEWS_DIMS},
                "rationale": rationale[:1000], "model": llm.extractor.model,
                "prompt_version": cfg.prompt_version,
                "input_hash": _hash(cfg.prompt_version, batch[aid]), "run_id": run_id,
                "created_at": now,
            })  # fmt: skip
        upsert(conn, news_risk_score, rows, key_cols=["activity_id"])
        stats.news_scored += len(rows)


# -- transcripts -----------------------------------------------------------------------------------


def score_transcripts(
    conn: Connection, client: LLMClient, llm: LLMConfig, cfg: TranscriptScoreConfig,
    company_id: int, company: str, run_id: int | None, now: datetime, stats: Stats,
) -> None:  # fmt: skip
    have = dict(conn.execute(
        select(transcript_tone.c.doc_id, transcript_tone.c.input_hash)
        .where(transcript_tone.c.company_id == company_id)
    ).all())  # fmt: skip
    docs = conn.execute(
        select(source_document.c.doc_id, source_document.c.accession_no,
               filing_section.c.section_id, filing_section.c.text_hash)
        .join(filing_section, filing_section.c.doc_id == source_document.c.doc_id)
        .where(source_document.c.company_id == company_id,
               source_document.c.source_type == "TRANSCRIPT")
    ).all()  # fmt: skip
    for d in docs:
        input_hash = _hash(cfg.prompt_version, d.text_hash)
        if have.get(d.doc_id) == input_hash:
            stats.transcripts_unchanged += 1
            continue
        chunks = conn.execute(
            select(section_chunk.c.chunk_index, section_chunk.c.text)
            .where(section_chunk.c.section_id == d.section_id)
            .order_by(section_chunk.c.chunk_index)
        ).all()  # fmt: skip
        scores: dict[int, Any] = {}
        texts = {c.chunk_index: c.text for c in chunks}
        quarter = d.accession_no.rsplit("-", 1)[-1]
        for start in range(0, len(chunks), cfg.chunks_per_call):
            batch = chunks[start : start + cfg.chunks_per_call]
            user = f"Company: {company}\nEarnings call {quarter}\n\n" + "\n\n".join(
                data_block("chunk", c.text, id=f"C{c.chunk_index}") for c in batch
            )
            out = _call(conn, client, llm, "transcript_tone", cfg.prompt_version, user,
                        ChunkScores, now)  # fmt: skip
            if out is None:
                continue
            ids = {c.chunk_index for c in batch}
            for s in out.chunks:
                key = s.chunk.strip().lstrip("C")
                if key.isdigit() and int(key) in ids:
                    scores[int(key)] = s
        stats.chunks_attempted += len(chunks)
        stats.chunks += len(scores)
        weights = {i: len(texts[i]) for i in scores}
        upsert(conn, transcript_tone, [{
            "doc_id": d.doc_id, "company_id": company_id,
            "overall_tone": weighted_mean(scores, weights, "tone", -1.0),
            **{dim: weighted_mean(scores, weights, dim) for dim in TONE_DIMS},
            "n_chunks": len(scores), "n_chunks_attempted": len(chunks),
            "model": llm.extractor.model, "prompt_version": cfg.prompt_version,
            # Only a complete scoring is final; otherwise the next run tries again (chunk calls
            # that succeeded come back from the LLM cache).
            "input_hash": input_hash if len(scores) == len(chunks) else "",
            "run_id": run_id, "created_at": now,
        }], key_cols=["doc_id"])  # fmt: skip
        # Flags: verbatim quotes only, from the chunks with the most liability cues first.
        flags = []
        for i in sorted(scores, key=lambda i: (-scores[i].liability_cues, i)):
            for f in scores[i].flags:
                if is_grounded(f.quote, texts[i]):
                    flags.append((f.category, f.quote[:1000], i))
                else:
                    stats.flags_dropped += 1
        flags = list(dict.fromkeys(flags))[: cfg.max_flags]
        conn.execute(delete(transcript_flag).where(transcript_flag.c.doc_id == d.doc_id))
        if flags:
            conn.execute(insert(transcript_flag), [
                {"doc_id": d.doc_id, "flag_index": n, "category": c, "flag_text": q,
                 "chunk_index": i}
                for n, (c, q, i) in enumerate(flags, start=1)
            ])  # fmt: skip
        stats.flags += len(flags)
        stats.transcripts_scored += 1


def run_scores_stage(
    companies: Sequence[CompanyConfig],
    llm: tuple[LLMClient, LLMConfig] | None,
    cfg: ScoresConfig,
    engine: Engine,
    *,
    run_id: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    if llm is None:
        return [TaskResult(c.ticker, "scores", ok=False, detail="LLM not configured")
                for c in companies]  # fmt: skip
    client, llm_cfg = llm
    results: list[TaskResult] = []
    for c in companies:
        try:
            stats = Stats()
            with short_transactions(engine) as conn:
                row = company_row(conn, c)
                stamp = now()
                score_news(conn, client, llm_cfg, cfg.news, row["company_id"],
                           row["legal_name"], run_id, stamp, stats)  # fmt: skip
                score_transcripts(conn, client, llm_cfg, cfg.transcript, row["company_id"],
                                  row["legal_name"], run_id, stamp, stats)  # fmt: skip
            log.info("%s: scores %s", c.ticker, stats.__dict__)
            detail = (
                f"news {stats.news_scored} scored/{stats.news_unchanged} unchanged, "
                f"transcripts {stats.transcripts_scored} scored, {stats.flags} flags"
            )
            results.append(TaskResult(c.ticker, "scores", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: scores stage failed", c.ticker)
            results.append(TaskResult(c.ticker, "scores", ok=False, detail=str(exc)))
    return results
