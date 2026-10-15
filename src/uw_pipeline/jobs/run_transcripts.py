"""Phase 4 `transcripts` stage: earnings call transcripts from Alpha Vantage.

Each transcript becomes a `source_document` (source_type TRANSCRIPT) with one `filing_section`
("TRANSCRIPT", one line per speaker turn) and its chunks, so Phase 5 can cite it like a filing.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, select

from uw_pipeline.clients.alphavantage import (
    AlphaVantageClient,
    PremiumRequiredError,
    QuotaExhaustedError,
    public_url,
)
from uw_pipeline.config import CompanyConfig
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_documents import _store_sections
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.sections import Section
from uw_pipeline.schema import ingest_state, metric_value, source_document

log = logging.getLogger(__name__)

QUARTERS = 4
# An unpublished transcript is asked for again after this long.
RETRY_EMPTY_AFTER = timedelta(days=7)


def recent_quarters(conn: Connection, company_id: int, n: int = QUARTERS) -> list[tuple[str, str]]:
    """(label like "2026Q2", period_end) for the company's latest n fiscal quarters, using the
    company's own fiscal labels from metric_value."""
    rows = conn.execute(
        select(metric_value.c.fiscal_year, metric_value.c.fiscal_quarter,
               metric_value.c.period_end)
        .where(
            and_(
                metric_value.c.company_id == company_id,
                metric_value.c.period_type == "Q",
                metric_value.c.metric_code == "revenue",
            )
        )
        .order_by(metric_value.c.fiscal_year.desc(), metric_value.c.fiscal_quarter.desc())
        .limit(n)
    )  # fmt: skip
    return [(f"{fy}Q{fq}", end) for fy, fq, end in rows]


def transcript_text(turns: Sequence[dict[str, Any]]) -> str:
    """One line per speaker turn: "Name (Title): text"."""
    lines = []
    for t in turns:
        who = t.get("speaker") or "Unknown"
        title = t.get("title")
        label = f"{who} ({title})" if title and title != who else who
        lines.append(f"{label}: {' '.join((t.get('content') or '').split())}")
    return "\n".join(lines)


def _state(conn: Connection, company_id: int, key: str) -> tuple[str, datetime] | None:
    row = conn.execute(
        select(ingest_state.c.content_hash, ingest_state.c.updated_at).where(
            and_(ingest_state.c.company_id == company_id, ingest_state.c.source_key == key)
        )
    ).first()
    if row is None:
        return None
    stamp = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    return row.content_hash, stamp


def process_company(
    cfg: CompanyConfig, av: AlphaVantageClient, conn: Connection, now: datetime
) -> dict[str, int]:
    row = company_row(conn, cfg)
    company_id = row["company_id"]
    stats = {"stored": 0, "already": 0, "unavailable": 0}
    for quarter, period_end in recent_quarters(conn, company_id):
        accn = f"AV-{cfg.ticker}-{quarter}"
        have = conn.execute(
            select(source_document.c.doc_id).where(
                and_(
                    source_document.c.accession_no == accn,
                    source_document.c.content_hash.is_not(None),
                )
            )
        ).first()
        if have is not None:
            stats["already"] += 1
            continue
        symbols = cfg.symbols()
        # The symbols tried are part of the key: adding one retries quarters found empty.
        state_key = f"av_transcript:{quarter}" + ("" if symbols == [cfg.ticker]
                                                  else ":" + "+".join(symbols))  # fmt: skip
        state = _state(conn, company_id, state_key)
        if state and state[0] == "EMPTY" and now - state[1] < RETRY_EMPTY_AFTER:
            stats["unavailable"] += 1
            continue

        symbol, raw_key = cfg.ticker, ""
        payload: Any = {}
        for symbol in symbols:
            payload, raw_key = av.transcript(symbol, quarter)
            if payload.get("transcript"):
                break
        turns = payload.get("transcript") or []
        if not turns:
            upsert(
                conn,
                ingest_state,
                [{"company_id": company_id, "source_key": state_key,
                  "content_hash": "EMPTY", "updated_at": now}],
                key_cols=["company_id", "source_key"],
            )  # fmt: skip
            stats["unavailable"] += 1
            continue

        text = transcript_text(turns)
        params = {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol, "quarter": quarter}
        upsert(
            conn,
            source_document,
            [
                {
                    "company_id": company_id,
                    "source_type": "TRANSCRIPT",
                    "form_type": "EARNINGS_CALL",
                    "doc_role": "PRIMARY",
                    "accession_no": accn,
                    "doc_name": "transcript",
                    "items": None,
                    "filed_at": now.date().isoformat(),  # first fetched; AV gives no call date
                    "period_end": period_end,
                    "url": public_url(params),
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "raw_path": raw_key,
                    "fetched_at": now,
                }
            ],
            key_cols=["accession_no", "doc_name"],
        )
        doc_id = conn.execute(
            select(source_document.c.doc_id).where(source_document.c.accession_no == accn)
        ).scalar_one()
        _store_sections(conn, doc_id, [Section("TRANSCRIPT", 0, len(text), text)])
        stats["stored"] += 1
    return stats


def run_transcripts_stage(
    companies: Sequence[CompanyConfig],
    av: AlphaVantageClient,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    stop: str | None = None
    for cfg in companies:
        if stop:
            results.append(TaskResult(cfg.ticker, "transcripts", ok=False, detail=stop))
            continue
        try:
            with engine.begin() as conn:
                stats = process_company(cfg, av, conn, now())
            log.info("%s: transcripts %s", cfg.ticker, stats)
            detail = ", ".join(f"{k}={v}" for k, v in stats.items())
            results.append(TaskResult(cfg.ticker, "transcripts", ok=True, detail=detail))
        except QuotaExhaustedError as exc:
            # Work committed for earlier companies stays; the rest resumes on the next run.
            stop = f"skipped: {exc}; resumes next run"
            log.warning("%s: %s", cfg.ticker, stop)
            results.append(TaskResult(cfg.ticker, "transcripts", ok=False, detail=stop))
        except PremiumRequiredError as exc:
            results.append(TaskResult(cfg.ticker, "transcripts", ok=False, detail=str(exc)))
        except Exception as exc:
            log.exception("%s: transcripts stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "transcripts", ok=False, detail=str(exc)))
    return results
