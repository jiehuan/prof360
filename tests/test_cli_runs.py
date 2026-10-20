"""`uw-pipeline runs` shows each run's data-quality outcome next to its status: a SUCCEEDED
run can still have dq Errors, and a run without a dq stage says "not run"."""

from datetime import datetime

from sqlalchemy import create_engine, insert
from typer.testing import CliRunner

from tests.conftest import CONFIG_DIR, TEST_UA
from uw_pipeline.cli import app
from uw_pipeline.clients.db import ensure_schema
from uw_pipeline.schema import dq_finding, pipeline_run, pipeline_task

T0 = datetime(2026, 10, 16, 1, 0)


def test_runs_show_dq_outcome(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEC_USER_AGENT", TEST_UA)
    monkeypatch.setenv("CONFIG_DIR", str(CONFIG_DIR))
    db = tmp_path / "uw.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = create_engine(f"sqlite:///{db}")
    ensure_schema(engine)
    # run -> {dq task status per company}, findings (severity, count)
    runs = {
        1: ({}, {}),  # no dq stage
        2: ({"MAR": "SUCCEEDED", "JPM": "SUCCEEDED"}, {"Error": 1, "Warning": 3, "Info": 9}),
        3: ({"MAR": "SUCCEEDED"}, {"Info": 2}),  # checked, nothing to report
        4: ({"MAR": "SUCCEEDED", "JPM": "FAILED"}, {"Warning": 1}),
        5: ({"MAR": "SKIPPED"}, {}),  # dq skipped (an earlier stage failed)
        6: ({"MAR": "PENDING"}, {}),
    }
    with engine.begin() as conn:
        for run_id, (tasks, found) in runs.items():
            conn.execute(
                insert(pipeline_run).values(
                    run_id=run_id,
                    job="manual",
                    trigger="manual",
                    status="SUCCEEDED",
                    stages="[]",
                    tickers="[]",
                    starts=1,
                    started_at=T0,
                    heartbeat_at=T0,
                    finished_at=T0,
                )
            )
            for n, (ticker, status) in enumerate(tasks.items()):
                conn.execute(
                    insert(pipeline_task).values(
                        run_id=run_id,
                        ticker=ticker,
                        stage="dq",
                        ordinal=n,
                        status=status,
                        attempts=1,
                        updated_at=T0,
                    )
                )
            for severity, count in found.items():
                conn.execute(insert(dq_finding), [{
                    "run_id": run_id, "company_id": 1, "rule_id": "r", "severity": severity,
                    "message": "m", "created_at": T0} for _ in range(count)])  # fmt: skip
    result = CliRunner().invoke(app, ["runs"])
    assert result.exit_code == 0, result.output
    shown = {int(line.split()[0]): line for line in result.output.splitlines()
             if line.strip()[:1].isdigit()}  # fmt: skip
    assert "dq=not run " in shown[1]
    assert "dq={'Error': 1, 'Warning': 3} " in shown[2]
    assert "dq={'Error': 0, 'Warning': 0} " in shown[3]
    assert "dq={'Error': 0, 'Warning': 1} (1 of 2 companies checked)" in shown[4]
    assert "dq=not run (skipped=1)" in shown[5]
    assert "dq=pending" in shown[6]
