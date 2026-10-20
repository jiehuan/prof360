import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from tests.conftest import CONFIG_DIR, ROOT
from uw_pipeline.jobs.orchestrator import (
    Orchestrator,
    RunInProgressError,
    load_jobs_config,
)
from uw_pipeline.jobs.run_company import TaskResult
from uw_pipeline.schema import metadata, pipeline_run, pipeline_task

STAGES = ["company", "facts", "compute", "documents"]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def engine():
    e = create_engine("sqlite://")
    metadata.create_all(e)
    return e


def tasks(engine, run_id):
    with engine.connect() as c:
        return {
            (r.ticker, r.stage): (r.status, r.attempts)
            for r in c.execute(select(pipeline_task).where(pipeline_task.c.run_id == run_id))
        }


def test_jobs_config_is_valid() -> None:
    cfg = load_jobs_config(CONFIG_DIR)
    assert {"nightly", "morning", "news"} <= cfg.jobs.keys()


def test_success_and_stage_major_order(engine) -> None:
    calls = []
    orch = Orchestrator(engine, now=Clock())
    run_id, resumed = orch.start("nightly", STAGES[:2], ["A", "B"])

    def runner(stage, ticker):
        calls.append((stage, ticker))
        return TaskResult(ticker, stage, ok=True)

    out = orch.execute(run_id, runner)
    assert not resumed and out.status == "SUCCEEDED" and out.counts == {"SUCCEEDED": 4}
    assert calls == [("company", "A"), ("company", "B"), ("facts", "A"), ("facts", "B")]


def test_failed_prerequisite_skips_only_that_company(engine) -> None:
    orch = Orchestrator(engine, now=Clock(), max_attempts=1)
    run_id, _ = orch.start("nightly", STAGES, ["A", "B"])

    def runner(stage, ticker):
        return TaskResult(ticker, stage, ok=not (ticker == "A" and stage == "facts"), detail="x")

    out = orch.execute(run_id, runner)
    t = tasks(engine, run_id)
    assert out.status == "PARTIAL"
    assert t[("A", "facts")] == ("FAILED", 1)
    assert t[("A", "compute")][0] == "SKIPPED"
    assert t[("A", "documents")][0] == "SUCCEEDED"  # depends only on company
    assert all(s == "SUCCEEDED" for (tk, _), (s, _) in t.items() if tk == "B")


def test_transient_failure_is_retried_permanent_is_not(engine) -> None:
    orch = Orchestrator(engine, now=Clock(), max_attempts=2)
    run_id, _ = orch.start("nightly", ["company", "transcripts"], ["A"])
    seen: dict[str, int] = {}

    def runner(stage, ticker):
        seen[stage] = seen.get(stage, 0) + 1
        if stage == "company":
            return TaskResult(ticker, stage, ok=seen[stage] > 1, detail="HTTP 503")
        return TaskResult(ticker, stage, ok=False, detail="daily quota of 25 calls used")

    orch.execute(run_id, runner)
    assert seen == {"company": 2, "transcripts": 1}
    t = tasks(engine, run_id)
    assert t[("A", "company")] == ("SUCCEEDED", 2)


def test_live_run_blocks_a_second_start(engine) -> None:
    clock = Clock()
    orch = Orchestrator(engine, now=clock, stale_after=timedelta(minutes=30))
    orch.start("nightly", STAGES, ["A"])
    clock.t += timedelta(minutes=5)
    with pytest.raises(RunInProgressError):
        orch.start("nightly", STAGES, ["A"])
    # One run at a time across jobs (D-041): they share stages and tables.
    with pytest.raises(RunInProgressError):
        orch.start("news", ["news"], ["A"])


def test_resume_after_crash_runs_only_unfinished_tasks(engine) -> None:
    clock = Clock()
    orch = Orchestrator(engine, now=clock, stale_after=timedelta(minutes=30))
    run_id, _ = orch.start("nightly", STAGES[:3], ["A", "B"])
    calls = []

    def crashing(stage, ticker):
        calls.append((stage, ticker))
        if (stage, ticker) == ("facts", "A"):
            raise KeyboardInterrupt  # the process dies mid-task (not caught by the orchestrator)
        return TaskResult(ticker, stage, ok=True)

    with pytest.raises(KeyboardInterrupt):
        orch.execute(run_id, crashing)
    assert tasks(engine, run_id)[("A", "facts")][0] == "RUNNING"

    clock.t += timedelta(minutes=45)  # heartbeat is now stale
    calls.clear()
    resumed_id, resumed = orch.start("nightly", STAGES[:3], ["A", "B"])
    out = orch.execute(
        resumed_id, lambda s, t: (calls.append((s, t)), TaskResult(t, s, ok=True))[1]
    )
    assert (resumed_id, resumed, out.status) == (run_id, True, "SUCCEEDED")
    assert calls == [("facts", "A"), ("facts", "B"), ("compute", "A"), ("compute", "B")]
    with engine.connect() as c:
        run = c.execute(select(pipeline_run)).mappings().one()
    assert run["starts"] == 2 and json.loads(run["stats_json"])["tasks"] == {"SUCCEEDED": 6}


def test_dead_run_with_other_stages_is_closed_not_resumed(engine) -> None:
    clock = Clock()
    orch = Orchestrator(engine, now=clock)
    old, _ = orch.start("nightly", STAGES, ["A"])
    clock.t += timedelta(hours=2)
    new, resumed = orch.start("nightly", ["company"], ["A"])
    assert new != old and not resumed
    with engine.connect() as c:
        status = c.execute(
            select(pipeline_run.c.status).where(pipeline_run.c.run_id == old)
        ).scalar_one()
    assert status == "FAILED"


def test_sigkill_and_restart_continue_the_run(tmp_path: Path) -> None:
    """The Phase 6 acceptance scenario with a real process: kill -9 mid-run, start again."""
    db, log = tmp_path / "uw.db", tmp_path / "calls.log"
    cmd = [sys.executable, "-m", "tests.kill_helper", str(db), str(log), "1"]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:  # let two tasks finish, then kill during the third
        if log.exists() and log.read_text().count("done") >= 2:
            break
        time.sleep(0.05)
    time.sleep(0.1)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    done_before = log.read_text().count("done")
    time.sleep(1.2)  # heartbeat older than the 1 s stale limit
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60, check=True)
    _, resumed, status = out.stdout.split()
    assert (resumed, status) == ("True", "SUCCEEDED")
    lines = log.read_text().splitlines()
    finished = [ln for ln in lines if ln.startswith("done")]
    assert len(set(finished)) == 6  # every task completed
    assert len(finished) == 6  # and none that had finished ran again
    assert done_before >= 2


def test_old_or_different_dead_runs_are_closed_not_resumed(engine) -> None:
    """Review 2026-10-14: a dead nightly run was resumed the next night (its finished tasks
    were then never refreshed), and a run over other companies was resumed too."""
    clock = Clock()
    orch = Orchestrator(engine, now=clock, resume_within=timedelta(hours=12))
    old, _ = orch.start("nightly", STAGES, ["A", "B"])
    clock.t += timedelta(hours=23)  # next night: dead and too old
    new, resumed = orch.start("nightly", STAGES, ["A", "B"])
    assert new != old and not resumed
    clock.t += timedelta(hours=1)  # `new` died; a start for one company only
    other, resumed = orch.start("nightly", STAGES, ["A"])
    assert other not in (old, new) and not resumed
    with engine.connect() as c:
        status = dict(c.execute(select(pipeline_run.c.run_id, pipeline_run.c.status)).all())
    assert (status[old], status[new], status[other]) == ("FAILED", "FAILED", "RUNNING")


def test_heartbeat_is_refreshed_during_a_long_task(tmp_path) -> None:
    import time

    from sqlalchemy import create_engine

    from uw_pipeline.schema import metadata

    eng = create_engine(f"sqlite:///{tmp_path / 'hb.db'}")
    metadata.create_all(eng)
    orch = Orchestrator(eng, heartbeat_every=timedelta(seconds=0.05))
    run_id, _ = orch.start("nightly", ["company"], ["A"])
    beats = []

    def slow(stage, ticker):
        with eng.connect() as c:
            before = c.execute(select(pipeline_run.c.heartbeat_at)).scalar_one()
        time.sleep(0.4)
        with eng.connect() as c:
            beats.append((before, c.execute(select(pipeline_run.c.heartbeat_at)).scalar_one()))
        return TaskResult(ticker, stage, ok=True)

    assert orch.execute(run_id, slow).status == "SUCCEEDED"
    assert beats[0][1] > beats[0][0]  # refreshed while the task was still running


def _concurrency_runner(slow_stage: str, fail: set[tuple[str, str]] = frozenset()):
    import threading

    lock, state = threading.Lock(), {"now": 0, "peak": {}}

    def runner(stage, ticker):
        with lock:
            state["now"] += 1
            state["peak"][stage] = max(state["peak"].get(stage, 0), state["now"])
        time.sleep(0.2 if stage == slow_stage else 0.01)
        with lock:
            state["now"] -= 1
        return TaskResult(ticker, stage, ok=(ticker, stage) not in fail, detail="x")

    return runner, state


def test_parallel_stage_runs_companies_together(tmp_path) -> None:
    # Phase 15i: a stage's companies run in threads; later stages still wait for their own
    # company's prerequisites. (A file database: tasks write their status from several threads.)
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    metadata.create_all(engine)
    orch = Orchestrator(engine, now=Clock(), max_attempts=1, parallel={"facts": 3},
                        allow_parallel=True)  # fmt: skip
    run_id, _ = orch.start("nightly", STAGES[:3], ["A", "B", "C"])
    runner, state = _concurrency_runner("facts", fail={("B", "facts")})
    started = time.monotonic()
    out = orch.execute(run_id, runner)
    assert time.monotonic() - started < 0.5  # three 0.2 s tasks at once, not one after another
    assert state["peak"] == {"company": 1, "facts": 3, "compute": 1}
    t = tasks(engine, run_id)
    assert out.status == "PARTIAL" and t[("B", "compute")][0] == "SKIPPED"
    assert t[("A", "compute")][0] == t[("C", "compute")][0] == "SUCCEEDED"


def test_parallel_only_on_a_server_database(engine) -> None:
    # SQLite allows one writer: one company at a time. (scripts/test_mssql.sh --all runs this
    # test on SQL Server, where the stage runs in parallel.)
    sqlite = engine.dialect.name == "sqlite"
    orch = Orchestrator(engine, now=Clock(), parallel={"facts": 3})
    assert orch.allow_parallel is not sqlite
    run_id, _ = orch.start("nightly", STAGES[:2], ["A", "B", "C"])
    runner, state = _concurrency_runner("facts")
    assert orch.execute(run_id, runner).status == "SUCCEEDED"
    assert state["peak"]["facts"] == (1 if sqlite else 3)


def test_short_transaction_stages_run_in_parallel_on_a_sqlite_file(tmp_path) -> None:
    # D-052: insights and metricdetail keep no transaction open while waiting for the model, so
    # on a SQLite file their companies run together; other stages stay one at a time.
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    metadata.create_all(engine)
    orch = Orchestrator(engine, now=Clock(), max_attempts=1,
                        parallel={"facts": 3, "insights": 3})  # fmt: skip
    assert not orch.allow_parallel
    run_id, _ = orch.start("nightly", ["company", "facts", "insights"], ["A", "B", "C"])
    runner, state = _concurrency_runner("insights")
    assert orch.execute(run_id, runner).status == "SUCCEEDED"
    assert state["peak"]["insights"] == 3 and state["peak"]["facts"] == 1
