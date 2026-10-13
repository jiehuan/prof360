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
