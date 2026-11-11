"""Logging setup: readable text locally, one JSON object per line in production
(LOG_FORMAT=json), with run/company/stage context on every record.

Concise by default (D-089): INFO carries the run, each stage's outcome, and warnings/errors;
per-company detail is DEBUG (`-v` or LOG_LEVEL=DEBUG). Known conditions nobody needs to act on
are notes (`note`, D-092): DEBUG, counted in the stage's line, kept in pipeline_task.detail.
With LOG_DIR set (the image sets /data/logs on the PVC), each run also writes a readable text
file there, kept LOG_KEEP_DAYS days and shown by `uw-pipeline logs`."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context")


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields (e.g. run_id, ticker, stage) to every log record in this block."""
    token = _context.set(_context.get({}) | fields)
    try:
        yield
    finally:
        _context.reset(token)


_notes: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("log_notes", default=None)


def note(logger: logging.Logger, msg: str, *args: Any) -> None:
    """A known, expected condition nobody needs to act on (D-092), e.g. an 8-K without its press
    release or a SIC code in no industry group. DEBUG in the log; kept with the task
    (pipeline_task.detail) and counted in the stage's line. A condition someone must act on
    is a warning."""
    logger.debug(msg, *args)
    notes = _notes.get()
    if notes is not None:
        notes.append(msg % args if args else msg)


@contextmanager
def collect_notes() -> Iterator[list[str]]:
    """Gather the notes of the code run in this block (one pipeline task)."""
    notes: list[str] = []
    token = _notes.set(notes)
    try:
        yield notes
    finally:
        _notes.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = _context.get({})
        return True


# Records the CLI already prints itself (the run summary): written to the run's log file only.
FILE_ONLY = {"file_only": True}


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **getattr(record, "ctx", {}),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", {})
        prefix = " ".join(f"{k}={v}" for k, v in ctx.items())
        line = super().format(record)
        return f"{line} [{prefix}]" if prefix else line


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_KEEP_DAYS = 30


def _level(verbose: bool) -> int:
    if verbose:
        return logging.DEBUG
    name = os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO"
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise ValueError(f"LOG_LEVEL={name!r} is not a logging level (DEBUG, INFO, WARNING)")
    return level


def setup_logging(*, json_format: bool, verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter())
    handler.addFilter(_ConsoleFilter())
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_level(verbose))
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# Libraries that log every request (or token) at INFO: their warnings and errors still show.
# azure covers the ADLS and identity clients (D-092).
NOISY_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "azure")


def log_dir() -> Path | None:
    """Where run logs are written and read (LOG_DIR); None when file logging is off."""
    value = os.environ.get("LOG_DIR", "").strip()
    return Path(value) if value else None


def keep_days() -> int:
    return int(os.environ.get("LOG_KEEP_DAYS", "") or DEFAULT_KEEP_DAYS)


def run_log_path(directory: Path, job: str, run_id: int) -> Path:
    """A resumed run appends to the file it started; a new run gets today's (UTC) date."""
    existing = sorted(directory.glob(f"*_{job}_run{run_id}.log"))
    if existing:
        return existing[0]
    return directory / f"{datetime.now(UTC):%Y-%m-%d}_{job}_run{run_id}.log"


def prune_logs(directory: Path, days: int) -> list[Path]:
    """Delete run logs older than `days` (by modification time)."""
    cutoff = time.time() - days * 86400
    removed = []
    for path in directory.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:  # removed meanwhile, or not ours to delete
            continue
    return removed


@contextmanager
def run_log_file(job: str, run_id: int) -> Iterator[Path | None]:
    """Also write this run's records to a text file under LOG_DIR (no-op when unset)."""
    directory = log_dir()
    if directory is None:
        yield None
        return
    directory.mkdir(parents=True, exist_ok=True)
    prune_logs(directory, keep_days())
    path = run_log_path(directory, job, run_id)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.addFilter(_ContextFilter())
    handler.setFormatter(TextFormatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        handler.close()


# -- reading (uw-pipeline logs) ------------------------------------------------------------------

_RECORD = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")
_RUN_FILE = re.compile(r"^(\d{4}-\d\d-\d\d)_(.+)_run(\d+)\.log$")


def run_logs(directory: Path) -> list[tuple[Path, str, int]]:
    """(path, job, run_id) of the run logs in `directory`, oldest run first."""
    found = []
    for path in directory.glob("*.log"):
        m = _RUN_FILE.match(path.name)
        if m:
            found.append((path, m.group(2), int(m.group(3))))
    return sorted(found, key=lambda f: f[2])


def read_records(
    path: Path, *, min_level: int = logging.INFO, ticker: str | None = None
) -> Iterator[str]:
    """The file's records at `min_level` or above (a traceback stays with its record),
    optionally only those naming `ticker` (in the message or its ticker= context)."""
    wanted = re.compile(rf"(?<![\w.]){re.escape(ticker)}(?![\w.])") if ticker else None
    record: list[str] = []

    def keep(lines: list[str]) -> bool:
        m = _RECORD.match(lines[0])
        level = logging.getLevelNamesMapping()[m.group(1)] if m else logging.INFO
        return level >= min_level and (wanted is None or bool(wanted.search(lines[0])))

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if _RECORD.match(line) and record:
                if keep(record):
                    yield from record
                record = []
            record.append(line)
    if record and keep(record):
        yield from record
