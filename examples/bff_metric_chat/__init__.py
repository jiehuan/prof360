"""Reference FastAPI implementation of the metric chat in the BFF (Phase 19.D, D-093).

For the BFF team to read and adapt: the pipeline never runs it in production. It shows the
two endpoints (ask, report), the suggested questions, the database reads and writes against
the objects of deploy/sql/bff_metric_chat.sql, per-user rate limiting and error handling.
`uw_metric_chat` is the folder the pipeline exports (scripts/export_metric_chat.py).
"""
