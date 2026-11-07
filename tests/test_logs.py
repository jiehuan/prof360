import json
import logging

from uw_pipeline.logs import JsonFormatter, _ContextFilter, log_context


def test_json_log_carries_context() -> None:
    record = logging.LogRecord("uw", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    with log_context(run_id=7, ticker="MAR"), log_context(stage="facts"):
        _ContextFilter().filter(record)
    data = json.loads(JsonFormatter().format(record))
    assert data["msg"] == "hello x" and data["level"] == "INFO"
    assert (data["run_id"], data["ticker"], data["stage"]) == (7, "MAR", "facts")


def _write(path, lines) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_run_log_file_appends_on_resume_and_prunes(tmp_path, monkeypatch) -> None:
    """D-089: with LOG_DIR set a run also writes a text file; a resumed run appends to it, and
    files older than LOG_KEEP_DAYS are removed. Console-only filtering: FILE_ONLY records go
    to the file, not the stream."""
    import os
    import time

    from uw_pipeline.logs import FILE_ONLY, run_log_file, setup_logging

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_KEEP_DAYS", "30")
    old = tmp_path / "2026-09-01_nightly_run1.log"
    old.write_text("old\n")
    stale = time.time() - 31 * 86400
    os.utime(old, (stale, stale))
    setup_logging(json_format=False)
    log = logging.getLogger("uw_pipeline.test")
    with run_log_file("nightly", 7) as path, log_context(run_id=7):
        log.info("first start")
        log.debug("detail")  # below the default level
        log.info("summary", extra=FILE_ONLY)
    with run_log_file("nightly", 7) as again:
        log.warning("resumed")
    assert path == again and path is not None and path.name.endswith("_nightly_run7.log")
    assert not old.exists()
    text = path.read_text()
    assert "INFO uw_pipeline.test: first start [run_id=7]" in text
    assert "detail" not in text and "summary" in text and "WARNING" in text
    log.info("after the run")  # the file handler is gone
    assert "after the run" not in path.read_text()


def test_no_log_dir_writes_nothing(tmp_path, monkeypatch) -> None:
    from uw_pipeline.logs import run_log_file

    monkeypatch.delenv("LOG_DIR", raising=False)
    with run_log_file("nightly", 1) as path:
        assert path is None


def test_logs_command_filters(tmp_path) -> None:
    from typer.testing import CliRunner

    from uw_pipeline.cli import app

    _write(tmp_path / "2026-11-06_nightly_run173.log", [
        "2026-11-06 23:30:00,000 INFO uw_pipeline: run 173 started [run_id=173 job=nightly]",
        "2026-11-06 23:31:00,000 DEBUG uw_pipeline.jobs.run_xbrl: MAR: 812 XBRL facts loaded "
        "[run_id=173 job=nightly ticker=MAR stage=facts]",
        "2026-11-06 23:32:00,000 ERROR uw_pipeline.jobs.run_xbrl: M: facts stage failed "
        "[run_id=173 job=nightly ticker=M stage=facts]",
        "Traceback (most recent call last):",
        "ValueError: boom",
        "2026-11-06 23:33:00,000 WARNING uw_pipeline.jobs.orchestrator: stage facts done in "
        "3m00s: 1 succeeded, 1 failed (M), 0 skipped [run_id=173 job=nightly]",
    ])  # fmt: skip
    _write(tmp_path / "2026-11-07_morning_run174.log", [
        "2026-11-07 05:30:00,000 INFO uw_pipeline: run 174 started [run_id=174 job=morning]",
    ])  # fmt: skip
    (tmp_path / "notes.txt").write_text("not a log")

    def run(*args: str) -> list[str]:
        result = CliRunner().invoke(app, ["logs", "--dir", str(tmp_path), *args])
        assert result.exit_code == 0, result.output
        return result.output.splitlines()[1:]  # after the "== path" header

    listed = CliRunner().invoke(app, ["logs", "--dir", str(tmp_path), "--list"]).output
    assert [line.split()[0] for line in listed.splitlines()] == ["173", "174"]
    assert run() == ["2026-11-07 05:30:00,000 INFO uw_pipeline: run 174 started "
                     "[run_id=174 job=morning]"]  # fmt: skip
    nightly = run("--job", "nightly")
    assert len(nightly) == 5 and "DEBUG" not in "".join(nightly)  # traceback kept
    assert run("--run", "173", "--level", "debug") == run("--job", "nightly", "--level", "DEBUG")
    assert len(run("--run", "173", "--level", "debug")) == 6
    errors = run("--run", "173", "--level", "warning", "--ticker", "M")
    assert errors[0].endswith("[run_id=173 job=nightly ticker=M stage=facts]")
    assert len(errors) == 4  # the failed task with its traceback, and the stage line naming M
    assert run("--run", "173", "--ticker", "MAR") == []  # its line is DEBUG
    assert run("--run", "173", "--tail", "1")[0].startswith("2026-11-06 23:33")
    missing = CliRunner().invoke(app, ["logs", "--dir", str(tmp_path), "--run", "9"])
    assert missing.exit_code == 1
