"""Phase 4 `news` stage: fetch Alpha Vantage news, de-duplicate, write activity/news_item."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, Engine, and_, delete, func, insert, select, update

from uw_pipeline.clients.alphavantage import (
    AlphaVantageClient,
    PremiumRequiredError,
    QuotaExhaustedError,
)
from uw_pipeline.clients.llm import LLMClient
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.llm import LLMConfig, LLMUnavailableError, short_transactions
from uw_pipeline.enrich.news_events import event_pairs
from uw_pipeline.enrich.news_relevance import judge
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.news import (
    Doc,
    NewsConfig,
    NewsItem,
    UnionFind,
    about_company,
    near_duplicates,
    parse_feed,
)
from uw_pipeline.schema import activity, ingest_state, news_item, news_risk_score

log = logging.getLogger(__name__)

PAGE_SIZE = 1000  # Alpha Vantage NEWS_SENTIMENT maximum


def load_news_config(config_dir: Path) -> NewsConfig:
    with (config_dir / "news.yaml").open(encoding="utf-8") as f:
        return NewsConfig.model_validate(yaml.safe_load(f))


def fetch_items(
    av: AlphaVantageClient,
    ticker: str,
    since: datetime,
    cfg: NewsConfig,
    aliases: Sequence[str] = (),
) -> tuple[list[tuple[NewsItem, str]], int]:
    """(item, raw landing key) pairs since `since`, following pages; and items below the
    relevance threshold."""
    out: list[tuple[NewsItem, str]] = []
    irrelevant = 0
    time_from = since
    while True:
        payload, raw_key = av.news(ticker, time_from)
        items, dropped = parse_feed(payload, ticker, cfg, aliases)
        out += [(i, raw_key) for i in items]
        irrelevant += dropped
        feed = payload.get("feed") or []
        if len(feed) < PAGE_SIZE:
            return out, irrelevant
        last = datetime.strptime(feed[-1]["time_published"], "%Y%m%dT%H%M%S")
        if last <= time_from:
            return out, irrelevant  # no progress; avoid looping on one timestamp
        time_from = last


@dataclass
class DedupStats:
    fetched: int = 0
    irrelevant: int = 0
    pruned: int = 0  # stored articles removed by the relevance rules or the LLM verdict
    judged: int = 0  # articles given an LLM relevance verdict this run
    url_duplicates: int = 0
    near_duplicates: int = 0
    event_duplicates: int = 0
    new_activities: int = 0
    attached_to_existing: int = 0
    llm_invented_ids: int = 0


def _existing_docs(conn: Connection, company_id: int, since: datetime) -> list[Doc]:
    rows = conn.execute(
        select(activity.c.activity_id, activity.c.title, activity.c.av_summary,
               activity.c.published_at).where(
            and_(activity.c.company_id == company_id, activity.c.published_at >= since)
        )
    )  # fmt: skip
    return [Doc(f"a{r.activity_id}", r.title, r.av_summary or "", r.published_at) for r in rows]


def dedup_and_store(
    conn: Connection,
    company_id: int,
    company_name: str,
    fetched: Sequence[tuple[NewsItem, str]],
    cfg: NewsConfig,
    *,
    llm: tuple[LLMClient, LLMConfig] | None,
    now: datetime,
) -> DedupStats:
    stats = DedupStats(fetched=len(fetched))
    known = set(
        conn.execute(
            select(news_item.c.url_hash).where(news_item.c.company_id == company_id)
        ).scalars()
    )
    # Tier 1: same normalised URL, against stored items and within this batch.
    new: dict[str, tuple[NewsItem, str]] = {}
    for item, raw in fetched:
        if item.url_hash in known or item.url_hash in new:
            stats.url_duplicates += 1
            continue
        new[item.url_hash] = (item, raw)
    if not new:
        return stats

    window = timedelta(hours=cfg.dedup.window_hours)
    keys = {f"n{i}": h for i, h in enumerate(new)}
    items = {k: new[h][0] for k, h in keys.items()}
    earliest = min(i.published_at for i in items.values())
    existing = _existing_docs(conn, company_id, earliest - window)
    docs = existing + [Doc(k, i.title, i.summary, i.published_at) for k, i in items.items()]

    def usable(a: str, b: str) -> bool:
        return not (a.startswith("a") and b.startswith("a"))  # never merge stored activities

    # Tier 2: near-identical text.
    near = UnionFind(d.key for d in docs)
    for a, b in near_duplicates(docs, cfg.dedup):
        if usable(a, b):
            near.union(a, b)
    groups = UnionFind(d.key for d in docs)
    for d in docs:
        groups.union(d.key, near.find(d.key))
    # Tier 3: same event, different articles.
    if llm and cfg.dedup.llm_event_grouping:
        client, llm_cfg = llm
        sources = {k: i.source for k, i in items.items()}
        try:
            pairs, invented = event_pairs(
                docs, sources, company=company_name, client=client, spec=llm_cfg.extractor,
                prompt_version=cfg.dedup.llm_prompt_version,
                batch_size=cfg.dedup.llm_batch_size, window=window, conn=conn,
                now=lambda: now,
            )  # fmt: skip
        except LLMUnavailableError as exc:
            log.error("LLM unavailable for news grouping; tiers 1-2 only: %s", exc)
            pairs, invented = [], 0
        stats.llm_invented_ids += invented
        for a, b in pairs:
            if usable(a, b) and not (
                groups.find(a).startswith("a") and groups.find(b).startswith("a")
            ):
                groups.union(a, b)

    # One activity per group: an existing one if the group has it, else a new one whose
    # canonical article is the earliest (then most relevant) new item.
    members: dict[str, list[str]] = {}
    for d in docs:
        members.setdefault(groups.find(d.key), []).append(d.key)
    for keys_in_group in members.values():
        new_keys = [k for k in keys_in_group if k.startswith("n")]
        if not new_keys:
            continue
        stored = sorted(int(k[1:]) for k in keys_in_group if k.startswith("a"))
        ordered = sorted(new_keys, key=lambda k: (items[k].published_at, -items[k].relevance))
        if stored:
            activity_id = stored[0]
            stats.attached_to_existing += len(ordered)
        else:
            canon = items[ordered[0]]
            inserted = conn.execute(
                insert(activity).values(
                    company_id=company_id,
                    title=canon.title,
                    published_at=canon.published_at,
                    source_name=canon.source,
                    url=canon.url,
                    url_hash=canon.url_hash,
                    av_summary=canon.summary,
                    av_sentiment_score=canon.ticker_sentiment_score,
                    av_sentiment_label=canon.ticker_sentiment_label,
                    av_relevance_score=canon.relevance,
                    source_count=0,
                    first_seen_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key
            assert inserted is not None
            activity_id = int(inserted[0])
            stats.new_activities += 1
        rows: list[dict[str, Any]] = []
        for k in ordered:
            item, raw = new[keys[k]]
            if not stored and k == ordered[0]:
                method = "FIRST"
            elif near.find(k) == near.find(ordered[0]) or any(
                near.find(k) == near.find(f"a{s}") for s in stored
            ):
                method = "NEAR_DUP"
            else:
                method = "LLM_EVENT"
            if method == "NEAR_DUP":
                stats.near_duplicates += 1
            elif method == "LLM_EVENT":
                stats.event_duplicates += 1
            rows.append(
                {
                    "company_id": company_id,
                    "activity_id": activity_id,
                    "url_hash": item.url_hash,
                    "url": item.url,
                    "title": item.title,
                    "summary": item.summary,
                    "source": item.source,
                    "source_domain": item.source_domain,
                    "authors": item.authors,
                    "published_at": item.published_at,
                    "relevance": item.relevance,
                    "ticker_sentiment_score": item.ticker_sentiment_score,
                    "ticker_sentiment_label": item.ticker_sentiment_label,
                    "overall_sentiment_score": item.overall_sentiment_score,
                    "overall_sentiment_label": item.overall_sentiment_label,
                    "dedup_method": method,
                    "raw_path": raw,
                    "fetched_at": now,
                }
            )
        conn.execute(insert(news_item), rows)
        count = conn.execute(
            select(func.count()).where(news_item.c.activity_id == activity_id)
        ).scalar_one()
        conn.execute(
            update(activity)
            .where(activity.c.activity_id == activity_id)
            .values(source_count=count, updated_at=now)
        )
    return stats


def _remove(conn: Connection, company_id: int, bad_ids: set[int]) -> int:
    """Delete articles; an activity whose canonical article goes is dropped whole (its headline
    is that article's), and activities left without articles are removed."""
    if not bad_ids:
        return 0
    rows = conn.execute(
        select(news_item.c.news_id, news_item.c.activity_id, news_item.c.dedup_method)
        .where(news_item.c.company_id == company_id)
    ).all()  # fmt: skip
    dead = {r.activity_id for r in rows if r.news_id in bad_ids and r.dedup_method == "FIRST"}
    gone = sorted(bad_ids | {r.news_id for r in rows if r.activity_id in dead})
    for i in range(0, len(gone), 500):
        conn.execute(delete(news_item).where(news_item.c.news_id.in_(gone[i : i + 500])))
    counts: dict[int, int] = dict(
        conn.execute(
            select(news_item.c.activity_id, func.count())
            .where(news_item.c.company_id == company_id)
            .group_by(news_item.c.activity_id)
        ).all()
    )  # fmt: skip
    acts = conn.execute(
        select(activity.c.activity_id, activity.c.source_count)
        .where(activity.c.company_id == company_id)
    ).all()  # fmt: skip
    empty = [a.activity_id for a in acts if a.activity_id not in counts]
    for i in range(0, len(empty), 500):
        chunk = empty[i : i + 500]
        # Scores reference the activity (FK, enforced on SQL Server): remove them first.
        conn.execute(delete(news_risk_score).where(news_risk_score.c.activity_id.in_(chunk)))
        conn.execute(delete(activity).where(activity.c.activity_id.in_(chunk)))
    for a in acts:
        n = counts.get(a.activity_id)
        if n is not None and n != a.source_count:
            conn.execute(update(activity).where(activity.c.activity_id == a.activity_id)
                         .values(source_count=n))  # fmt: skip
    return len(gone)


def prune_unrelated(
    conn: Connection, company_id: int, aliases: Sequence[str], cfg: NewsConfig
) -> int:
    """Apply the current relevance rules to stored articles. Idempotent; a no-op once clean."""
    if not aliases:
        return 0
    rows = conn.execute(
        select(news_item.c.news_id, news_item.c.title, news_item.c.summary)
        .where(news_item.c.company_id == company_id)
    ).all()  # fmt: skip
    bad = {r.news_id for r in rows
           if not about_company(r.title, r.summary or "", aliases, cfg.mention)}  # fmt: skip
    return _remove(conn, company_id, bad)


def judge_and_prune(
    conn: Connection,
    company_id: int,
    company_name: str,
    cfg: NewsConfig,
    llm: tuple[LLMClient, LLMConfig] | None,
    now: datetime,
) -> tuple[int, int]:
    """LLM verdict for articles not judged yet; remove those not kept. (judged, removed)"""
    rc = cfg.relevance_llm
    if not rc.enabled or llm is None:
        return 0, 0
    todo = conn.execute(
        select(news_item.c.news_id, news_item.c.title, news_item.c.summary)
        .where(news_item.c.company_id == company_id, news_item.c.relevance_verdict.is_(None))
    ).all()  # fmt: skip
    if todo:
        client, llm_cfg = llm
        try:
            verdicts = judge(
                [(r.news_id, r.title, r.summary or "") for r in todo], company=company_name,
                client=client, spec=llm_cfg.extractor, prompt_version=rc.prompt_version,
                batch_size=rc.batch_size, conn=conn, now=lambda: now,
            )  # fmt: skip
        except LLMUnavailableError as exc:
            log.error("LLM unavailable for news relevance; articles kept unjudged: %s", exc)
            verdicts = {}
        for news_id, verdict in verdicts.items():
            conn.execute(update(news_item).where(news_item.c.news_id == news_id)
                         .values(relevance_verdict=verdict))  # fmt: skip
    bad = set(
        conn.execute(
            select(news_item.c.news_id).where(
                news_item.c.company_id == company_id,
                news_item.c.relevance_verdict.is_not(None),
                news_item.c.relevance_verdict.not_in(rc.keep),
            )
        ).scalars()
    )
    return len(todo), _remove(conn, company_id, bad)


def run_news_stage(
    companies: Sequence[CompanyConfig],
    av: AlphaVantageClient,
    engine: Engine,
    cfg: NewsConfig,
    *,
    llm: tuple[LLMClient, LLMConfig] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for company_cfg in companies:
        try:
            with short_transactions(engine) as conn:
                row = company_row(conn, company_cfg)
                latest = conn.execute(
                    select(func.max(news_item.c.published_at)).where(
                        news_item.c.company_id == row["company_id"]
                    )
                ).scalar_one_or_none()
                stamp = now()
                # A changed symbol list (e.g. an old ticker added) re-reads the full window.
                symbols = "+".join(company_cfg.symbols())
                known = conn.execute(
                    select(ingest_state.c.content_hash).where(
                        ingest_state.c.company_id == row["company_id"],
                        ingest_state.c.source_key == "av_news_symbols",
                    )
                ).scalar()
                # (no record yet: the list was the ticker alone, as before av_symbols existed)
                incremental = latest is not None and (known or company_cfg.ticker) == symbols
                since = (
                    latest - timedelta(days=cfg.overlap_days)
                    if latest is not None and incremental
                    else stamp.replace(tzinfo=None) - timedelta(days=cfg.lookback_days)
                )
                pruned = prune_unrelated(conn, row["company_id"], company_cfg.aliases(), cfg)
                fetched, irrelevant = [], 0
                for symbol in company_cfg.symbols():  # duplicates are removed by URL later
                    got, dropped = fetch_items(av, symbol, since, cfg, company_cfg.aliases())
                    fetched += got
                    irrelevant += dropped
                stats = dedup_and_store(
                    conn, row["company_id"], row["legal_name"], fetched, cfg, llm=llm, now=stamp
                )
                stats.irrelevant = irrelevant
                upsert(conn, ingest_state, [{"company_id": row["company_id"],
                                             "source_key": "av_news_symbols",
                                             "content_hash": symbols, "updated_at": stamp}],
                       key_cols=["company_id", "source_key"])  # fmt: skip
                stats.judged, removed = judge_and_prune(
                    conn, row["company_id"], row["legal_name"], cfg, llm, stamp
                )
                stats.pruned = pruned + removed
            log.info("%s: news %s", company_cfg.ticker, stats.__dict__)
            detail = f"{stats.new_activities} new activities from {stats.fetched} articles"
            results.append(TaskResult(company_cfg.ticker, "news", ok=True, detail=detail))
        except PremiumRequiredError:
            msg = "NEWS_SENTIMENT needs a premium Alpha Vantage plan"
            log.error("%s: %s", company_cfg.ticker, msg)
            results.append(TaskResult(company_cfg.ticker, "news", ok=False, detail=msg))
        except QuotaExhaustedError as exc:
            log.warning("%s: %s; continuing next run", company_cfg.ticker, exc)
            results.append(TaskResult(company_cfg.ticker, "news", ok=False, detail=str(exc)))
        except Exception as exc:
            log.exception("%s: news stage failed", company_cfg.ticker)
            results.append(TaskResult(company_cfg.ticker, "news", ok=False, detail=str(exc)))
    return results
