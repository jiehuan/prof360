"""Run/task state machine with resume (Phase 6, decisions D-034).

A run is one execution of a job (a list of stages) for a list of companies. Each
(company, stage) is a task: PENDING -> RUNNING -> SUCCEEDED | FAILED | SKIPPED.

- Resume: starting a job whose latest run is still RUNNING but whose heartbeat is stale (the
  process died, e.g. the pod was killed) continues that run. SUCCEEDED and SKIPPED tasks are
  kept; interrupted (RUNNING) tasks and FAILED tasks with attempts left run again.
- Lock: a RUNNING run with a fresh heartbeat blocks a second start of the same job.
- Dependencies: a stage whose prerequisite stage failed or was skipped for that company is
  skipped (other companies are unaffected).
- Retry: a failed task is retried immediately while it has attempts left.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Engine, func, insert, select, update

from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.logs import log_context
from uw_pipeline.schema import pipeline_run, pipeline_task

log = logging.getLogger(__name__)


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes for timezone-aware columns; all stored times are UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Prerequisite stages per stage (only those that are part of the same run count).
DEPENDS: dict[str, tuple[str, ...]] = {
    "company": (),
    "facts": ("company",),
    "compute": ("facts",),
    "documents": ("company",),
    "transcripts": ("compute",),
    "news": ("company",),
    "scores": ("company",),
    "textmetrics": ("compute", "documents"),
    "signals": ("compute", "documents"),
    "insights": ("signals", "textmetrics"),
    "metricdetail": ("compute", "insights"),
    # Publishes whatever is current, so it runs even when an LLM stage failed.
    "publish": ("compute",),
    "dq": ("compute",),
}

DONE = ("SUCCEEDED", "SKIPPED")


class JobSpec(BaseModel):
    stages: list[str]
    # Re-fetch SEC metadata on every run instead of reusing the same-day raw cache.
    refresh_sec: bool = False


class JobsConfig(BaseModel):
    jobs: dict[str, JobSpec]
    stale_after_minutes: int = 30
    max_attempts: int = 2
    resume_within_hours: int = 12
    heartbeat_minutes: int = 5


def load_jobs_config(config_dir: Path) -> JobsConfig:
    with (config_dir / "jobs.yaml").open(encoding="utf-8") as f:
        cfg = JobsConfig.model_validate(yaml.safe_load(f))
    for name, job in cfg.jobs.items():
        unknown = set(job.stages) - DEPENDS.keys()
        if unknown:
            raise ValueError(f"job {name}: unknown stages {sorted(unknown)}")
    return cfg


class RunInProgressError(Exception):
    pass


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    status: str
    resumed: bool
    counts: dict[str, int]


Runner = Callable[[str, str], TaskResult]  # (stage, ticker) -> result


class Orchestrator:
    def __init__(
        self,
        engine: Engine,
        *,
        stale_after: timedelta = timedelta(minutes=30),
        max_attempts: int = 2,
        resume_within: timedelta = timedelta(hours=12),
        heartbeat_every: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.stale_after = stale_after
        self.max_attempts = max_attempts
        self.resume_within = resume_within
        self.heartbeat_every = heartbeat_every
        self.now = now

    # -- starting / resuming -------------------------------------------------------------------

    def start(
        self,
        job: str,
        stages: Sequence[str],
        tickers: Sequence[str],
        *,
        trigger: str = "manual",
        resume: bool = True,
    ) -> tuple[int, bool]:
        """(run_id, resumed).

        One run at a time across all jobs: jobs share stages and tables, so a live run of any
        job (fresh heartbeat) blocks the start. A dead run of the same job is resumed only if it
        is recent and covers the same stages and companies; every other dead run is closed, so
        tonight's nightly never "continues" last night's (decisions D-041)."""
        now = self.now()
        with self.engine.begin() as conn:
            running = conn.execute(
                select(pipeline_run)
                .where(pipeline_run.c.status == "RUNNING")
                .order_by(pipeline_run.c.run_id.desc())
            ).mappings().all()  # fmt: skip
            for r in running:
                beat = _aware(r["heartbeat_at"])
                if now - beat < self.stale_after:
                    raise RunInProgressError(
                        f"run {r['run_id']} of job '{r['job']}' is still running "
                        f"(heartbeat {beat:%Y-%m-%d %H:%M:%S} UTC)"
                    )
            latest = next((r for r in running if r["job"] == job), None)
            if (
                resume
                and latest is not None
                and json.loads(latest["stages"]) == list(stages)
                and json.loads(latest["tickers"]) == list(tickers)
                and now - _aware(latest["started_at"]) < self.resume_within
            ):
                run_id = int(latest["run_id"])
                conn.execute(
                    update(pipeline_run)
                    .where(pipeline_run.c.run_id == run_id)
                    .values(starts=latest["starts"] + 1, heartbeat_at=now)
                )
                conn.execute(
                    update(pipeline_task)
                    .where(pipeline_task.c.run_id == run_id,
                           pipeline_task.c.status == "RUNNING")
                    .values(status="PENDING", detail="interrupted; resumed", updated_at=now)
                )  # fmt: skip
                # Skips are re-evaluated: their prerequisite may succeed this time.
                conn.execute(
                    update(pipeline_task)
                    .where(pipeline_task.c.run_id == run_id,
                           pipeline_task.c.status == "SKIPPED")
                    .values(status="PENDING", updated_at=now)
                )  # fmt: skip
                self._close_dead(conn, [r for r in running if r["run_id"] != run_id], now)
                log.warning("resuming run %d of job '%s' (heartbeat was %s)",
                            run_id, job, latest["heartbeat_at"])  # fmt: skip
                return run_id, True
            # Dead runs that won't be resumed are closed so they no longer block.
            self._close_dead(conn, running, now)

            result = conn.execute(
                insert(pipeline_run).values(
                    job=job, trigger=trigger, status="RUNNING", stages=json.dumps(list(stages)),
                    tickers=json.dumps(list(tickers)), starts=1, started_at=now,
                    heartbeat_at=now,
                )
            )  # fmt: skip
            pk = result.inserted_primary_key
            assert pk is not None
            run_id = int(pk[0])
            rows, ordinal = [], 0
            for stage in stages:  # stage-major order: one stage for all companies, then the next
                for ticker in tickers:
                    ordinal += 1
                    rows.append(
                        {"run_id": run_id, "ticker": ticker, "stage": stage, "ordinal": ordinal,
                         "status": "PENDING", "attempts": 0, "updated_at": now}
                    )  # fmt: skip
            conn.execute(insert(pipeline_task), rows)
        return run_id, False

    @staticmethod
    def _close_dead(conn: Any, runs: Sequence[Any], now: datetime) -> None:
        for r in runs:
            conn.execute(
                update(pipeline_run)
                .where(pipeline_run.c.run_id == r["run_id"])
                .values(status="FAILED", finished_at=now,
                        stats_json=json.dumps({"abandoned": True}))
            )  # fmt: skip
            log.warning("closed dead run %d of job '%s'", r["run_id"], r["job"])

    # -- executing -----------------------------------------------------------------------------

    def _set(self, task_id: int, run_id: int, **values: Any) -> None:
        now = self.now()
        with self.engine.begin() as conn:
            conn.execute(
                update(pipeline_task)
                .where(pipeline_task.c.task_id == task_id)
                .values(updated_at=now, **values)
            )
            conn.execute(
                update(pipeline_run).where(pipeline_run.c.run_id == run_id).values(heartbeat_at=now)
            )

    def _heartbeat(self, run_id: int) -> threading.Event:
        """Keep the run's heartbeat fresh while one long task runs; set the event to stop."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_every.total_seconds()):
                try:
                    with self.engine.begin() as conn:
                        conn.execute(update(pipeline_run).where(pipeline_run.c.run_id == run_id)
                                     .values(heartbeat_at=self.now()))  # fmt: skip
                except Exception:  # e.g. SQLite busy while the stage writes; next beat retries
                    log.debug("heartbeat update failed", exc_info=True)

        threading.Thread(target=beat, name=f"heartbeat-{run_id}", daemon=True).start()
        return stop

    def execute(self, run_id: int, runner: Runner) -> RunOutcome:
        with self.engine.connect() as conn:
            run = (
                conn.execute(select(pipeline_run).where(pipeline_run.c.run_id == run_id))
                .mappings()
                .one()
            )
            tasks = (
                conn.execute(
                    select(pipeline_task)
                    .where(pipeline_task.c.run_id == run_id)
                    .order_by(pipeline_task.c.ordinal)
                )
                .mappings()
                .all()
            )
        stages_in_run = set(json.loads(run["stages"]))
        status: dict[tuple[str, str], str] = {(t["ticker"], t["stage"]): t["status"] for t in tasks}

        for t in tasks:
            key = (t["ticker"], t["stage"])
            if status[key] in DONE:
                continue
            if status[key] == "FAILED" and t["attempts"] >= self.max_attempts:
                continue
            blocked = [
                d for d in DEPENDS[t["stage"]]
                if d in stages_in_run and status.get((t["ticker"], d)) != "SUCCEEDED"
            ]  # fmt: skip
            if blocked:
                status[key] = "SKIPPED"
                self._set(t["task_id"], run_id, status="SKIPPED",
                          detail=f"prerequisite not succeeded: {', '.join(blocked)}")  # fmt: skip
                continue
            attempts = t["attempts"]
            while True:
                attempts += 1
                self._set(t["task_id"], run_id, status="RUNNING", attempts=attempts,
                          started_at=self.now())  # fmt: skip
                with log_context(run_id=run_id, ticker=t["ticker"], stage=t["stage"]):
                    beating = self._heartbeat(run_id)
                    try:
                        result = runner(t["stage"], t["ticker"])
                    except Exception as exc:  # stage functions normally catch their own errors
                        log.exception("task crashed")
                        result = TaskResult(t["ticker"], t["stage"], ok=False, detail=repr(exc))
                    finally:
                        beating.set()
                outcome = "SUCCEEDED" if result.ok else "FAILED"
                self._set(t["task_id"], run_id, status=outcome, detail=result.detail[:2000],
                          finished_at=self.now())  # fmt: skip
                status[key] = outcome
                if result.ok or attempts >= self.max_attempts or not _retryable(result):
                    break
                log.warning("%s %s failed (%s); retrying", t["ticker"], t["stage"], result.detail)
        return self._finish(run_id)

    def _finish(self, run_id: int) -> RunOutcome:
        now = self.now()
        with self.engine.begin() as conn:
            counts: dict[str, int] = dict(
                conn.execute(
                    select(pipeline_task.c.status, func.count())
                    .where(pipeline_task.c.run_id == run_id)
                    .group_by(pipeline_task.c.status)
                ).all()
            )
            run = (
                conn.execute(select(pipeline_run).where(pipeline_run.c.run_id == run_id))
                .mappings()
                .one()
            )
            if counts.get("FAILED") or counts.get("SKIPPED"):
                status = "PARTIAL" if counts.get("SUCCEEDED") else "FAILED"
            else:
                status = "SUCCEEDED"
            stats = json.loads(run["stats_json"] or "{}") | {"tasks": counts}
            conn.execute(
                update(pipeline_run)
                .where(pipeline_run.c.run_id == run_id)
                .values(status=status, finished_at=now, heartbeat_at=now,
                        stats_json=json.dumps(stats))
            )  # fmt: skip
        return RunOutcome(run_id, status, run["starts"] > 1, {k: int(v) for k, v in counts.items()})

    def add_stats(self, run_id: int, extra: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            current = conn.execute(
                select(pipeline_run.c.stats_json).where(pipeline_run.c.run_id == run_id)
            ).scalar_one()
            merged = json.loads(current or "{}") | extra
            conn.execute(
                update(pipeline_run)
                .where(pipeline_run.c.run_id == run_id)
                .values(stats_json=json.dumps(merged))
            )


# Failures that won't go away by retrying within the same run.
_PERMANENT = ("premium", "quota", "not set", "not configured", "not in company table")


def _retryable(result: TaskResult) -> bool:
    detail = result.detail.lower()
    return not any(p in detail for p in _PERMANENT)
