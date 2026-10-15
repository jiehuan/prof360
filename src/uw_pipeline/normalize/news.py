"""Alpha Vantage news feed -> items, and de-duplication (decisions D-029).

Tier 1: same article URL after normalisation (tracking parameters, scheme, www, AMP, trailing
        slash removed).
Tier 2: syndicated copies, i.e. near-identical title/summary within a time window
        (word 3-gram shingle Jaccard).
Tier 3: different articles about the same event (LLM, see enrich/news_events.py).
Items are grouped with union-find; each group becomes one `activity`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import BaseModel


class DedupConfig(BaseModel):
    window_hours: int = 72
    title_similarity: float = 0.8
    summary_similarity: float = 0.8
    title_similarity_with_summary: float = 0.5
    strip_params_prefixes: list[str] = []
    llm_event_grouping: bool = True
    llm_batch_size: int = 120
    llm_prompt_version: str = "news_events_v1"


class MentionFilter(BaseModel):
    require_mention: bool = True
    # Regexes (case-insensitive); "{alias}" is replaced by each company alias. A match means the
    # company is only the actor (analyst rating, fund holding), not the subject.
    exclude_patterns: list[str] = []


class RelevanceLLM(BaseModel):
    enabled: bool = True
    batch_size: int = 100
    prompt_version: str = "news_relevance_v1"
    keep: list[str] = ["SUBJECT", "UNCLASSIFIED"]


class NewsConfig(BaseModel):
    lookback_days: int = 30
    overlap_days: int = 1
    min_relevance: float = 0.3
    mention: MentionFilter = MentionFilter()
    relevance_llm: RelevanceLLM = RelevanceLLM()
    dedup: DedupConfig = DedupConfig()


def about_company(title: str, summary: str, aliases: Sequence[str], cfg: MentionFilter) -> bool:
    """The article is about the company: it names it, and not only as analyst or investor."""
    text = f"{title}\n{summary}"
    # Case-sensitive (proper nouns; "Chase" is not "chase"), plus the all-caps form.
    names = sorted({re.escape(v) for a in aliases if a for v in (a, a.upper())}, key=len,
                   reverse=True)  # fmt: skip
    if not names:
        return True
    name_re = "(?:" + "|".join(names) + ")"
    if cfg.require_mention and not re.search(rf"(?<![A-Za-z]){name_re}(?![a-z])", text):
        return False
    for pattern in cfg.exclude_patterns:
        if re.search(pattern.replace("{alias}", name_re), title, re.I):
            return False
    return True


@dataclass(frozen=True)
class NewsItem:
    url: str
    url_hash: str
    title: str
    summary: str
    source: str
    source_domain: str
    published_at: datetime
    authors: str
    overall_sentiment_score: float | None
    overall_sentiment_label: str | None
    relevance: float
    ticker_sentiment_score: float | None
    ticker_sentiment_label: str | None


# -- URLs --------------------------------------------------------------------------------------

_AMP = re.compile(r"(/amp/?$|\.amp$|/amp(?=/))")


def normalize_url(url: str, strip_prefixes: Sequence[str] = ()) -> str:
    """Canonical form used for exact de-duplication (not for display)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = _AMP.sub("", parts.path).rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not any(k.lower().startswith(p) for p in strip_prefixes)
        )
    )
    return f"{host}{path}" + (f"?{query}" if query else "")


def url_hash(url: str, strip_prefixes: Sequence[str] = ()) -> str:
    return hashlib.sha256(normalize_url(url, strip_prefixes).encode()).hexdigest()


# -- feed parsing ------------------------------------------------------------------------------


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_feed(
    payload: dict[str, Any], ticker: str, cfg: NewsConfig, aliases: Sequence[str] = ()
) -> tuple[list[NewsItem], int]:
    """Items relevant to `ticker` (AV relevance >= min_relevance, and about the company per
    `about_company` when aliases are given) and the count dropped."""
    items, dropped = [], 0
    for raw in payload.get("feed") or []:
        ts = next(
            (t for t in raw.get("ticker_sentiment") or [] if t.get("ticker", "").upper() == ticker),
            None,
        )
        relevance = _float(ts.get("relevance_score")) if ts else None
        title, summary = (raw.get("title") or "").strip(), (raw.get("summary") or "").strip()
        if (
            relevance is None
            or relevance < cfg.min_relevance
            or (aliases and not about_company(title, summary, aliases, cfg.mention))
        ):
            dropped += 1
            continue
        items.append(
            NewsItem(
                url=raw["url"],
                url_hash=url_hash(raw["url"], cfg.dedup.strip_params_prefixes),
                title=title,
                summary=summary,
                source=raw.get("source") or "",
                source_domain=raw.get("source_domain") or "",
                published_at=datetime.strptime(raw["time_published"], "%Y%m%dT%H%M%S"),
                authors=", ".join(raw.get("authors") or []),
                overall_sentiment_score=_float(raw.get("overall_sentiment_score")),
                overall_sentiment_label=raw.get("overall_sentiment_label"),
                relevance=relevance,
                ticker_sentiment_score=_float(ts.get("ticker_sentiment_score")) if ts else None,
                ticker_sentiment_label=ts.get("ticker_sentiment_label") if ts else None,
            )
        )
    return items, dropped


# -- near-duplicate text -----------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
# A trailing " - Reuters" / " | Benzinga" style source tag added by syndicators.
_SOURCE_TAG = re.compile(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{1,40}$")


def normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(_SOURCE_TAG.sub("", title).lower()))


def shingles(text: str, n: int = 3) -> frozenset[str]:
    words = _WORD.findall(text.lower())
    if len(words) < n:
        return frozenset([" ".join(words)]) if words else frozenset()
    return frozenset(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class Doc:
    """Anything that can be clustered: a new item, or an existing activity's canonical text."""

    key: str
    title: str
    summary: str
    published_at: datetime


class UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self.parent = {k: k for k in keys}

    def find(self, k: str) -> str:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def near_duplicates(docs: Sequence[Doc], cfg: DedupConfig) -> list[tuple[str, str]]:
    """Pairs of keys whose text is near-identical within the time window."""
    window = timedelta(hours=cfg.window_hours)
    prepared = sorted(
        (
            (d.published_at, d.key, shingles(normalize_title(d.title)), shingles(d.summary))
            for d in docs
        ),
        key=lambda x: x[0],
    )
    pairs = []
    for i, (t_i, k_i, title_i, summ_i) in enumerate(prepared):
        for t_j, k_j, title_j, summ_j in prepared[i + 1 :]:
            if t_j - t_i > window:
                break
            t_sim = jaccard(title_i, title_j)
            if t_sim >= cfg.title_similarity or (
                t_sim >= cfg.title_similarity_with_summary
                and jaccard(summ_i, summ_j) >= cfg.summary_similarity
            ):
                pairs.append((k_i, k_j))
    return pairs
