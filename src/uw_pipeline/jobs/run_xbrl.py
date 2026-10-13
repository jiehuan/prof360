"""Phase 2 stages: `facts` (XBRL -> xbrl_fact) and `compute` (xbrl_fact -> metric_value)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Connection, Engine, and_, delete, select, tuple_

from uw_pipeline.clients.sec import SecClient, pad_cik
from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import CompanyRegistry, Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.extract.filings import Filing, instance_name, periodic_filings
from uw_pipeline.frames import records
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.load.upsert import upsert
from uw_pipeline.normalize.periods import learn_calendar, standardize
from uw_pipeline.normalize.xbrl_facts import (
    FACT_COLUMNS,
    facts_from_companyfacts,
    facts_from_instance,
)
from uw_pipeline.schema import company, ingest_state, metric_definition, metric_value, xbrl_fact

log = logging.getLogger(__name__)

# metric_value.derivation values written by the compute stage.
RULE_DERIVATIONS = ("REPORTED", "DERIVED", "COMPUTED")

# companyfacts can lag or miss filings (Ford's Q2 2026 10-Q was still missing 2.5 months after
# filing), so recent filings absent from companyfacts are read from their own XBRL instance.
GAP_LOOKBACK_DAYS = 400


class CompanyNotLoadedError(Exception):
    pass


def company_row(conn: Connection, cfg: CompanyConfig) -> dict[str, Any]:
    if cfg.cik is None:
        raise CompanyNotLoadedError(f"{cfg.ticker}: no pinned CIK")
    row = conn.execute(select(company).where(company.c.cik == pad_cik(cfg.cik))).mappings().first()
    if row is None:
        raise CompanyNotLoadedError(f"{cfg.ticker}: not in company table; run --stage company")
    return dict(row)


def _company_registry(
    registry: Registry, industry: IndustryConfig, cfg: CompanyConfig, row: dict[str, Any]
) -> CompanyRegistry:
    return registry.for_company(cfg.ticker, industry.groups[row["industry_group"]])


def _fact_key(cik: str, r: dict[str, Any]) -> str:
    parts = [
        cik,
        r["taxonomy"],
        r["tag"],
        r["dims"],
        r["unit"],
        r["start"] or "",
        r["end"],
        r["accn"],
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _companyfacts_accns(payload: dict[str, Any]) -> set[str]:
    return {
        f["accn"]
        for tags in (payload.get("facts") or {}).values()
        for body in tags.values()
        for facts in body.get("units", {}).values()
        for f in facts
    }


def _state_hash(conn: Connection, company_id: int, key: str) -> str | None:
    return conn.execute(
        select(ingest_state.c.content_hash).where(
            and_(ingest_state.c.company_id == company_id, ingest_state.c.source_key == key)
        )
    ).scalar_one_or_none()


# -- facts stage -------------------------------------------------------------------------------


def load_company_facts(
    cfg: CompanyConfig,
    sec: SecClient,
    creg: CompanyRegistry,
    *,
    cik: str,
    today: date,
    history_years: int,
    force: bool,
) -> tuple[pd.DataFrame, str]:
    """All facts the company's metrics need, plus a fingerprint of the inputs."""
    refs = creg.tag_refs()
    std_refs = {r for r in refs if not r.needs_instance}
    inst_refs = refs - std_refs

    cf = sec.companyfacts(cik, force=force)
    frames = [facts_from_companyfacts(cf.payload, std_refs)]
    fingerprint = [cf.content_hash, *sorted(r.label() for r in refs)]

    cf_accns = _companyfacts_accns(cf.payload)
    since = today - timedelta(days=365 * history_years if inst_refs else GAP_LOOKBACK_DAYS)
    subs = sec.submissions(cik, force=force).payload
    gap_since = (today - timedelta(days=GAP_LOOKBACK_DAYS)).isoformat()
    for filing in periodic_filings(sec, cik, subs, since, force=force):
        gap = filing.accn not in cf_accns and filing.filed >= gap_since
        wanted = inst_refs | (std_refs if gap else set())
        if not wanted:
            continue
        if gap:
            log.info("%s: %s %s not in companyfacts; reading its XBRL instance",
                     cfg.ticker, filing.form, filing.accn)  # fmt: skip
        frame = _instance_facts(sec, cik, filing, wanted)
        if frame is not None:
            frames.append(frame)
            fingerprint.append(filing.accn)
    facts = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if facts.empty:
        facts = pd.DataFrame(columns=FACT_COLUMNS)
    digest = hashlib.sha256("\n".join(fingerprint).encode()).hexdigest()
    return facts, digest


def _instance_facts(
    sec: SecClient, cik: str, filing: Filing, refs: set[Any]
) -> pd.DataFrame | None:
    name = instance_name(sec.filing_index(cik, filing.accn), filing.primary_doc)
    if name is None:
        log.warning("CIK %s %s: no XBRL instance found", cik, filing.accn)
        return None
    doc = sec.filing_document(cik, filing.accn, name)
    return facts_from_instance(
        doc.payload, refs, accn=filing.accn, form=filing.form, filed=filing.filed
    )


def _fact_rows(cik: str, company_id: int, facts: pd.DataFrame) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for r in records(facts.astype(object).where(facts.notna(), None)):
        key = _fact_key(cik, r)
        rows[key] = {
            "fact_key": key,
            "company_id": company_id,
            "taxonomy": r["taxonomy"],
            "tag": r["tag"],
            "dims": r["dims"],
            "unit": r["unit"],
            "val": float(r["val"]),
            "start_date": r["start"],
            "end_date": r["end"],
            "fy": int(r["fy"]) if r["fy"] is not None else None,
            "fp": r["fp"],
            "form": r["form"],
            "accession_no": r["accn"],
            "filed_at": r["filed"],
            "frame": r["frame"],
            "source": r["source"],
        }
    return list(rows.values())


def run_facts_stage(
    companies: Sequence[CompanyConfig],
    sec: SecClient,
    registry: Registry,
    industry: IndustryConfig,
    engine: Engine,
    *,
    history_years: int = 6,
    force: bool = False,
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.connect() as conn:
                row = company_row(conn, cfg)
            creg = _company_registry(registry, industry, cfg, row)
            facts, digest = load_company_facts(
                cfg, sec, creg, cik=row["cik"], today=today(),
                history_years=history_years, force=force,
            )  # fmt: skip
            with engine.begin() as conn:
                if not force and _state_hash(conn, row["company_id"], "xbrl_facts") == digest:
                    log.info("%s: XBRL inputs unchanged; skipping load", cfg.ticker)
                    results.append(TaskResult(cfg.ticker, "facts", ok=True, detail="unchanged"))
                    continue
                rows = _fact_rows(row["cik"], row["company_id"], facts)
                upsert(conn, xbrl_fact, rows, key_cols=["fact_key"])
                upsert(
                    conn,
                    ingest_state,
                    [{"company_id": row["company_id"], "source_key": "xbrl_facts",
                      "content_hash": digest, "updated_at": now()}],
                    key_cols=["company_id", "source_key"],
                )  # fmt: skip
            log.info("%s: %d XBRL facts loaded", cfg.ticker, len(rows))
            results.append(TaskResult(cfg.ticker, "facts", ok=True, detail=f"{len(rows)} facts"))
        except Exception as exc:
            log.exception("%s: facts stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "facts", ok=False, detail=str(exc)))
    return results


# -- compute stage -----------------------------------------------------------------------------


def read_facts(conn: Connection, company_id: int) -> pd.DataFrame:
    rows = conn.execute(select(xbrl_fact).where(xbrl_fact.c.company_id == company_id)).mappings()
    facts = pd.DataFrame(
        [
            {
                "taxonomy": r["taxonomy"],
                "tag": r["tag"],
                "dims": r["dims"],
                "unit": r["unit"],
                "val": r["val"],
                "start": r["start_date"],
                "end": r["end_date"],
                "fy": r["fy"],
                "fp": r["fp"],
                "form": r["form"],
                "accn": r["accession_no"],
                "filed": r["filed_at"],
                "frame": r["frame"],
                "source": r["source"],
            }
            for r in rows
        ],
        columns=FACT_COLUMNS,
    )
    return facts


def drop_before(facts: pd.DataFrame, history_start: date | None) -> pd.DataFrame:
    """Drop facts whose period starts (or, for instants, ends) before `history_start`."""
    if history_start is None or facts.empty:
        return facts
    cutoff = history_start.isoformat()
    begins = facts["start"].where(facts["start"].notna(), facts["end"])
    return facts[begins >= cutoff].reset_index(drop=True)


def compute_company(
    facts: pd.DataFrame,
    fye_mmdd: str,
    creg: CompanyRegistry,
    history_start: date | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    # The fiscal-year label offset is learned from all facts, then pre-history is dropped.
    cal = learn_calendar(facts, fye_mmdd)
    facts = drop_before(facts, history_start)
    periods = standardize(facts, cal)
    return periods, compute_metrics(periods, creg)


def _definition_rows(registry: Registry) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for ticker in ["", *registry.overrides]:
        for m in registry._catalog(ticker):
            rows[m.code] = {
                "metric_code": m.code,
                "display_name": m.display_name,
                "description": m.description,
                "section": m.section,
                "unit": m.unit,
                "applies_to": ticker or "SETS",
                "is_display": m.display,
                "company_specific": m.company_specific,
            }
    return list(rows.values())


def run_compute_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    engine: Engine,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    with engine.begin() as conn:
        upsert(conn, metric_definition, _definition_rows(registry), key_cols=["metric_code"])
    results: list[TaskResult] = []
    for cfg in companies:
        try:
            with engine.connect() as conn:
                row = company_row(conn, cfg)
                facts = read_facts(conn, row["company_id"])
            creg = _company_registry(registry, industry, cfg, row)
            _, metrics = compute_company(
                facts, row["fiscal_year_end"] or "1231", creg, cfg.history_start
            )
            stamp = now()
            out: list[dict[str, Any]] = []
            for code, by_period in metrics.items():
                unit = next(m.unit for m in creg.metrics if m.code == code)
                for (ptype, fy, fq), v in by_period.items():
                    out.append(
                        {
                            "company_id": row["company_id"],
                            "metric_code": code,
                            "period_type": ptype,
                            "fiscal_year": fy,
                            "fiscal_quarter": fq,
                            "period_end": v.period_end,
                            "value": v.value,
                            "value_status": v.status,
                            "unit": unit,
                            "derivation": v.derivation,
                            "formula": v.formula[:300],
                            "source_tags": json.dumps(v.source_map())[:2000],
                            "source_accn": v.accn,
                            "computed_at": stamp,
                        }
                    )
            keys = ["company_id", "metric_code", "period_type", "fiscal_year", "fiscal_quarter"]
            with engine.begin() as conn:
                upsert(conn, metric_value, out, key_cols=keys)
                # Drop this company's rows the current rules no longer produce (not a full wipe).
                produced = {tuple(r[k] for k in keys[1:]) for r in out}
                # Only rows this stage owns; LLM-extracted / preliminary rows belong to the
                # text-metrics stage.
                existing = conn.execute(
                    select(*(metric_value.c[k] for k in keys[1:])).where(
                        metric_value.c.company_id == row["company_id"],
                        metric_value.c.derivation.in_(RULE_DERIVATIONS),
                    )
                ).all()
                stale = [tuple(e) for e in existing if tuple(e) not in produced]
                if stale:
                    conn.execute(
                        delete(metric_value).where(
                            metric_value.c.company_id == row["company_id"],
                            tuple_(*(metric_value.c[k] for k in keys[1:])).in_(stale),
                        )
                    )
            log.info("%s: %d metric values (%d stale removed)", cfg.ticker, len(out), len(stale))
            results.append(TaskResult(cfg.ticker, "compute", ok=True, detail=f"{len(out)} values"))
        except Exception as exc:
            log.exception("%s: compute stage failed", cfg.ticker)
            results.append(TaskResult(cfg.ticker, "compute", ok=False, detail=str(exc)))
    return results
