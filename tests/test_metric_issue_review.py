"""Users' reports of a wrong-looking metric value (Phase 19.C, D-092): pulled from the BFF's
tables by publish, grouped in `uw-pipeline review`, decided with a note, counted by dq."""

import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select

from tests import test_insights as ti
from tests.conftest import ROOT
from uw_pipeline.jobs.run_publish import has_report_tables, pull_issue_reports
from uw_pipeline.jobs.run_review import ReviewError, decide, pending
from uw_pipeline.quality.dq import ReviewQueue, review_queue
from uw_pipeline.schema import (
    company,
    metadata,
    metric_chat_context,
    metric_issue_report,
    review_decision,
)
from uw_pipeline.schema_app import app_metadata, pipeline_publish_map
from uw_pipeline.schema_bff import MetricIssueReport, UserMetricChat, bff_metadata

NOW = datetime(2026, 11, 7, 18, tzinfo=UTC)
APP_COMPANY, APP_METRIC = 501, 17


def _columns(sql: str, table: str) -> list[str]:
    body = sql.split(f"CREATE TABLE dbo_professional360.{table} (", 1)[1].split("\n);")[0]
    return re.findall(r"^\s+(\w+) ", body, re.M)


def test_bff_tables_match_their_sql() -> None:
    sql = (ROOT / "deploy" / "sql" / "bff_metric_chat.sql").read_text(encoding="utf-8")
    for table in (UserMetricChat, MetricIssueReport):
        assert _columns(sql, table.name) == [c.name for c in table.columns], table.name


def _env(with_bff: bool = True):
    pipe = create_engine("sqlite://")
    metadata.create_all(pipe)
    ti._seed(pipe)  # MAR, company_id 1, CIK 0001048286
    app = create_engine("sqlite://")
    app_metadata.create_all(app)
    if with_bff:
        bff_metadata.create_all(app)
    with app.begin() as c:
        c.execute(insert(pipeline_publish_map), [
            {"entity": "Company", "pipeline_key": "0001048286", "app_id": APP_COMPANY,
             "row_hash": "h", "published_at": NOW},
            {"entity": "metrics", "pipeline_key": "debt_to_equity", "app_id": APP_METRIC,
             "row_hash": "h", "published_at": NOW},
        ])  # fmt: skip
    with pipe.begin() as c:
        c.execute(insert(metric_chat_context).values(
            company_id=1, metric_code="debt_to_equity", context_version=1, context_hash="h",
            updated_at=NOW, context_json=json.dumps({
                "metric": {"name": "Debt-to-Equity", "unit": "ratio"},
                "periods": [{"period": "2026Q2", "value_id": "dp:2026Q2:debt_to_equity",
                             "status": "NM", "derivation": "COMPUTED",
                             "nm_reason": "Not meaningful when equity ≤ 0.", "accession": "a"}],
                "filings": [{"accession": "a", "url": "https://sec/mar-10q.htm"}],
                "data_points": [{"id": "dp:2026Q2:debt_to_equity", "value": None,
                                 "unit": "ratio"}]})))  # fmt: skip
    return pipe, app


def _report(app, user: int, source: str = "USER", period: str | None = "2026Q2", **kw) -> None:
    with app.begin() as c:
        turn = c.execute(insert(UserMetricChat).values(
            ConversationId=uuid.uuid4(), UserId=user, CompanyId=APP_COMPANY,
            metric_id=APP_METRIC, Question=f"question of {user}", Answer="a", Grounding="ok",
            CreatedAt=NOW)).inserted_primary_key[0]  # fmt: skip
        c.execute(insert(MetricIssueReport).values(
            UserId=user, CompanyId=APP_COMPANY, metric_id=APP_METRIC, TurnId=turn,
            Source=source, Period=period, CreatedAt=NOW, **kw))  # fmt: skip


def _pull(pipe, app) -> int:
    with app.connect() as a, pipe.begin() as p:
        row = dict(p.execute(select(company).where(company.c.company_id == 1)).mappings().one())
        return pull_issue_reports(a, p, row, NOW)


def test_reports_are_pulled_once_with_their_turn_and_no_user_id() -> None:
    pipe, app = _env()
    with app.connect() as c:
        assert has_report_tables(c)
    _report(app, 7, Comment="Negative equity? Looks odd")
    _report(app, 8, source="MODEL", IssueSummary="Equity is negative.")
    assert _pull(pipe, app) == 2
    assert _pull(pipe, app) == 0
    _report(app, 7, Comment="again")
    assert _pull(pipe, app) == 1
    with pipe.connect() as c:
        rows = c.execute(select(metric_issue_report).order_by("report_id")).mappings().all()
    assert [(r["metric_code"], r["source"], r["period"]) for r in rows] == [
        ("debt_to_equity", "USER", "2026Q2"), ("debt_to_equity", "MODEL", "2026Q2"),
        ("debt_to_equity", "USER", "2026Q2")]  # fmt: skip
    assert rows[0]["question"] == "question of 7" and rows[0]["comment"].startswith("Negative")
    assert rows[0]["user_hash"] == rows[2]["user_hash"] != rows[1]["user_hash"]
    assert len(rows[0]["user_hash"]) == 16 and "UserId" not in metric_issue_report.c


def test_without_the_bff_tables_nothing_is_pulled() -> None:
    _, app = _env(with_bff=False)
    with app.connect() as c:
        assert not has_report_tables(c)


def test_review_groups_reports_and_needs_a_note_to_approve() -> None:
    pipe, app = _env()
    _report(app, 7, Comment="Negative equity? Looks odd")
    _report(app, 8, Comment="Same here")
    _report(app, 9, source="MODEL", period="FY2025", IssueSummary="Check equity.")
    _pull(pipe, app)
    with pipe.connect() as c:
        items = [i for i in pending(c) if i.item_type == "REPORT"]
        assert [i.item_type for i in pending(c, "MODEL") if i.item_type == "REPORT"] == ["REPORT"]
    user, model = items
    assert (user.status, user.title, len(user.keys)) == ("USER", "Debt-to-Equity FY2026 Q2", 2)
    assert user.short.startswith("R-") and user.source == "https://sec/mar-10q.htm"
    assert "2 reports from 2 users: 2 by users, 0 by the chat" in user.evidence
    assert '"Same here"' in user.evidence and "Not meaningful when equity" in user.evidence
    assert (model.status, model.title) == ("MODEL", "Debt-to-Equity FY2025")
    with pipe.begin() as c, pytest.raises(ReviewError, match="add --note"):
        decide(c, user.short, "APPROVED", "Ann Lee", None, NOW)
    with pipe.begin() as c:
        decide(c, user.short, "APPROVED", "Ann Lee", "Equity basis wrong; override + D-0xx", NOW)
        keys = {k for (k,) in c.execute(select(review_decision.c.item_key))}
        assert keys == set(user.keys)
        assert [i.item_type for i in pending(c) if i.item_type == "REPORT"] == ["REPORT"]
    # A new report on the same value after the decision is a new item.
    _report(app, 10, Comment="Still odd")
    _pull(pipe, app)
    with pipe.connect() as c:
        again = [i for i in pending(c) if i.item_type == "REPORT" and i.status == "USER"]
    assert len(again) == 1 and len(again[0].keys) == 1 and again[0].short != user.short


def test_dq_counts_open_reports_per_metric() -> None:
    pipe, app = _env()
    _report(app, 7, Comment="x")
    _report(app, 8, source="MODEL", IssueSummary="y")
    _pull(pipe, app)
    cfg = ReviewQueue(severity="Info", statuses=["DISPUTED"])
    with pipe.connect() as c:
        [found] = review_queue(c, 1, cfg)
    assert found.metric_code == "debt_to_equity"
    assert "1 by users, 1 by the chat" in found.message
    with pipe.begin() as c:
        for i in [i for i in pending(c) if i.item_type == "REPORT"]:
            decide(c, i.short, "REJECTED", "Ann Lee", None, NOW)
        assert review_queue(c, 1, cfg) == []
