"""Phase 7 `dq` stage: evaluate data-quality rules and store findings for the run."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, delete, insert

from uw_pipeline.compute.coverage import load_notes
from uw_pipeline.compute.registry import Registry
from uw_pipeline.config import CompanyConfig, IndustryConfig
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.jobs.run_xbrl import company_row
from uw_pipeline.quality.dq import DqConfig, run_rules
from uw_pipeline.schema import dq_finding

log = logging.getLogger(__name__)


def run_dq_stage(
    companies: Sequence[CompanyConfig],
    registry: Registry,
    industry: IndustryConfig,
    cfg: DqConfig,
    config_dir: Path,
    engine: Engine,
    *,
    run_id: int | None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[TaskResult]:
    notes = load_notes(config_dir)
    results = []
    for company in companies:
        try:
            with engine.begin() as conn:
                row = company_row(conn, company)
                metrics = registry.for_company(
                    company.ticker, industry.groups[row["industry_group"]]
                ).metrics
                findings = run_rules(conn, row["company_id"], company.ticker, metrics,
                                     notes.get(company.ticker, {}), cfg, now)  # fmt: skip
                # Re-running the stage within a run replaces that run's findings.
                conn.execute(
                    delete(dq_finding).where(
                        dq_finding.c.company_id == row["company_id"],
                        dq_finding.c.run_id.is_(None)
                        if run_id is None
                        else dq_finding.c.run_id == run_id,
                    )
                )
                stamp = now()
                if findings:
                    conn.execute(
                        insert(dq_finding),
                        [{"run_id": run_id, "company_id": row["company_id"], "rule_id": f.rule_id,
                          "severity": f.severity, "metric_code": f.metric_code,
                          "period": f.period, "message": f.message[:1000], "created_at": stamp}
                         for f in findings],
                    )  # fmt: skip
            counts: dict[str, int] = {}
            for f in findings:
                counts[f.severity] = counts.get(f.severity, 0) + 1
            log.info("%s: dq %s", company.ticker, counts or "clean")
            detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "clean"
            # Findings are data, not stage failures: the task succeeds either way.
            results.append(TaskResult(company.ticker, "dq", ok=True, detail=detail))
        except Exception as exc:
            log.exception("%s: dq stage failed", company.ticker)
            results.append(TaskResult(company.ticker, "dq", ok=False, detail=str(exc)))
    return results
