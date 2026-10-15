"""Subprocess used by test_orchestrator: runs a job whose tasks sleep, logging each execution."""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine

from uw_pipeline.jobs.orchestrator import Orchestrator
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.schema import metadata

db, log_path, stale_seconds = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
engine = create_engine(f"sqlite:///{db}")
metadata.create_all(engine)
orch = Orchestrator(engine, stale_after=timedelta(seconds=stale_seconds))
run_id, resumed = orch.start("nightly", ["company", "facts", "compute"], ["AAA", "BBB"],
                             trigger="cron")  # fmt: skip


def runner(stage: str, ticker: str) -> TaskResult:
    with log_path.open("a") as f:
        f.write(f"start {stage} {ticker}\n")
    time.sleep(0.4)
    with log_path.open("a") as f:
        f.write(f"done {stage} {ticker}\n")
    return TaskResult(ticker, stage, ok=True)


outcome = orch.execute(run_id, runner)
print(run_id, resumed, outcome.status)
