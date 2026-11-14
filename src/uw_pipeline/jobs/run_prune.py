"""Raw snapshot retention (D-098): daily snapshots older than `keep_days` are deleted.

Only the prefixes `raw_retention` in config/jobs.yaml names are touched; archive documents
and transcripts, which later stages read back, are never listed there. In each folder the
newest file stays whatever its age, and a key a `source_document` row references stays.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, field_validator
from sqlalchemy import Engine, select

from uw_pipeline.clients.sec import pad_cik
from uw_pipeline.clients.storage import Storage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.schema import source_document

log = logging.getLogger(__name__)

_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_PLACEHOLDERS = ("{cik}", "{symbol}")


class RawRetention(BaseModel):
    keep_days: int = 7
    # Per-company folders: "{cik}" is each registrant CIK (10 digits, as in the keys),
    # "{symbol}" each Alpha Vantage symbol.
    company_prefixes: list[str] = []
    # Folders shared by all companies (pruned by `uw-pipeline prune-raw`).
    shared_prefixes: list[str] = []

    @field_validator("keep_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("keep_days must be at least 1")
        return v

    @field_validator("shared_prefixes")
    @classmethod
    def _no_placeholders(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if any(ph in p for ph in _PLACEHOLDERS)]
        if bad:
            raise ValueError(f"shared_prefixes take no placeholders: {bad}")
        return v


@dataclass
class PruneStats:
    files: int = 0
    deleted: int = 0


def file_date(key: str) -> date | None:
    """The fetch date a snapshot's file name starts with (2026-10-18.json,
    2026-10-25T035917Z.json, 2026-11-04-from-20261016T0938.json); None when it has none."""
    m = _DATE.match(key.rpartition("/")[2])
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def expired(keys: Iterable[str], cutoff: date) -> list[str]:
    """Keys dated before `cutoff`, except the newest dated file of each folder."""
    by_folder: dict[str, list[tuple[date, str]]] = {}
    for key in keys:
        d = file_date(key)
        if d is not None:
            by_folder.setdefault(key.rpartition("/")[0], []).append((d, key))
    out: list[str] = []
    for files in by_folder.values():
        files.sort()
        out.extend(key for d, key in files[:-1] if d < cutoff)
    return sorted(out)


def _referenced(engine: Engine, keys: Sequence[str]) -> set[str]:
    found: set[str] = set()
    with engine.connect() as conn:
        for i in range(0, len(keys), 500):
            found.update(conn.execute(
                select(source_document.c.raw_path)
                .where(source_document.c.raw_path.in_(keys[i:i + 500]))
            ).scalars())  # fmt: skip
    return found


def prune_prefixes(
    prefixes: Iterable[str],
    storage: Storage,
    engine: Engine,
    cutoff: date,
    *,
    dry_run: bool = False,
) -> PruneStats:
    """Delete expired snapshots under `prefixes`; returns files seen and deleted."""
    stats = PruneStats()
    for prefix in prefixes:
        keys = storage.list_keys(prefix)
        stats.files += len(keys)
        doomed = expired(keys, cutoff)
        keep = _referenced(engine, doomed) if doomed else set()
        for key in doomed:
            if key in keep:
                continue
            if not dry_run:
                storage.delete(key)
            stats.deleted += 1
    return stats


def company_prefixes(cfg: RawRetention, company: CompanyConfig) -> list[str]:
    ciks = company.registrant_ciks(pad_cik(company.cik)) if company.cik is not None else []
    out: list[str] = []
    for pattern in cfg.company_prefixes:
        if "{cik}" in pattern:
            out.extend(pattern.replace("{cik}", c) for c in ciks)
        elif "{symbol}" in pattern:
            out.extend(pattern.replace("{symbol}", s) for s in company.symbols())
        else:
            out.append(pattern)
    return out


def cutoff_for(cfg: RawRetention, now: datetime) -> date:
    return now.astimezone(UTC).date() - timedelta(days=cfg.keep_days)


def run_prune_stage(
    companies: Sequence[CompanyConfig],
    cfg: RawRetention,
    storage: Storage,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    cutoff = cutoff_for(cfg, now())
    results = []
    for company in companies:
        try:
            stats = prune_prefixes(company_prefixes(cfg, company), storage, engine, cutoff)
            log.debug("%s: pruned %d of %d raw files", company.ticker, stats.deleted,
                      stats.files)  # fmt: skip
            detail = f"deleted {stats.deleted} of {stats.files}"
            results.append(TaskResult(company.ticker, "prune", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: prune stage failed", company.ticker)
            results.append(TaskResult(company.ticker, "prune", ok=False, detail=str(exc)))
    return results
