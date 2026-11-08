"""The reference BFF for the metric chat (examples/bff_metric_chat, Phase 19.D, D-093): its
endpoints with a fake store and model, and its SQL store on SQL Server (MSSQL_TEST_URL)."""

import json
import os
import re
import uuid
from datetime import datetime

import pytest

pytest.importorskip("fastapi")

from examples.bff_metric_chat.api import ChatSettings, User, current_user
from examples.bff_metric_chat.main import create_app
from examples.bff_metric_chat.store import SqlChatStore, StoredContext, TurnOwner
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import ROOT
from uw_metric_chat import ANSWER_SCHEMA, ChatPromptRow, GatewayError, HistoryTurn
from uw_metric_chat.securegpt import Completion

CONTEXT = {
    "version": 1, "company": {"ticker": "GM"},
    "metric": {"code": "interest_coverage", "name": "Interest Coverage"},
    "periods": [{"period": "2025Q4", "label": "FY2025 Q4", "status": "NM"},
                {"period": "FY2025", "label": "FY2025", "status": "OK"}],
    "data_points": [
        {"id": "dp:FY2025:interest_coverage", "label": "Interest Coverage FY2025",
         "value": 4.0013755158, "unit": "ratio", "status": "OK"},
        {"id": "dp:FY2025:ebit", "label": "EBIT FY2025", "value": 2909e6, "unit": "USD",
         "status": "OK"},
    ],
}  # fmt: skip
PROMPT = ChatPromptRow(prompt_version="metric_chat_v1", system_text="RULES",
                       output_schema=ANSWER_SCHEMA, context_versions="1",
                       min_package_version="0.1.0")  # fmt: skip


def _answer(text: str, cites: list[str], **kw) -> dict:
    return {"answer": text, "citations": cites, "in_scope": True, "possible_issue": False,
            "issue_summary": None, "period": "FY2025"} | kw  # fmt: skip


class FakeStore:
    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.reports: list[dict] = []
        self.prompt_row: ChatPromptRow | None = PROMPT

    def context(self, company_id, metric_id):
        if (company_id, metric_id) != (51, 17):
            return None
        return StoredContext(json.dumps(CONTEXT), 1, datetime(2026, 11, 7))

    def prompt(self):
        return self.prompt_row

    def history(self, conversation_id, user_id, company_id, metric_id, limit):
        return [
            HistoryTurn(question=t["Question"], answer=t["Answer"])
            for t in self.turns
            if (t["ConversationId"], t["UserId"]) == (conversation_id, user_id)
        ][-limit:]

    def add_turn(self, row):
        self.turns.append(row)
        return len(self.turns)

    def turn_owner(self, turn_id):
        if not 1 <= turn_id <= len(self.turns):
            return None
        t = self.turns[turn_id - 1]
        return TurnOwner(t["UserId"], t["CompanyId"], t["metric_id"])

    def add_report(self, row):
        self.reports.append(row)
        return 900 + len(self.reports)


class FakeModel:
    def __init__(self, *outputs) -> None:
        self.outputs, self.seen = list(outputs), []

    async def complete_json(self, *, model, messages, schema, max_tokens=1500):
        self.seen.append(messages)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return Completion(out, json.dumps(out), model, 4000, 200, 900)


URL = "/api/companies/51/metrics/17/chat"


def _client(store, model, user_id=7, **settings) -> TestClient:
    app = create_app(store=store, client=model, settings=ChatSettings(**settings))
    app.dependency_overrides[current_user] = lambda: User(id=user_id)
    return TestClient(app)


def test_suggestions_name_the_n_m_period() -> None:
    with _client(FakeStore(), FakeModel()) as c:
        got = c.get(f"{URL}/suggestions").json()
        assert c.get("/api/companies/51/metrics/99/chat/suggestions").status_code == 404
    assert got == {"questions": ["How is Interest Coverage calculated?",
                                 "Why is Interest Coverage N/M for FY2025 Q4?",
                                 "Where does this number come from?"]}  # fmt: skip


def test_ask_answers_logs_the_turn_and_files_the_model_s_issue() -> None:
    store = FakeStore()
    model = FakeModel(
        _answer("FY2025 coverage was 4.0x on EBIT of $2.91B.",
                ["dp:FY2025:interest_coverage", "dp:FY2025:ebit"], possible_issue=True,
                issue_summary="Check the interest basis."),
        _answer("FY2025 EBIT was $2.91B.", ["dp:FY2025:ebit"]),
    )  # fmt: skip
    with _client(store, model) as c:
        first = c.post(URL, json={"message": "How is it calculated?"})
        assert first.status_code == 200, first.text
        body = first.json()
        second = c.post(URL, json={"conversationId": body["conversationId"],
                                   "message": "And EBIT?"}).json()  # fmt: skip
    assert body["status"] == "answered" and body["turnId"] == 1
    assert body["possibleIssue"] is True and body["issueSummary"] == "Check the interest basis."
    assert [x["period"] for x in body["citations"]] == ["FY2025", "FY2025"]
    assert set(body) == {"conversationId", "turnId", "status", "answer", "citations",
                         "possibleIssue", "issueSummary", "period"}  # fmt: skip
    turn = store.turns[0]
    assert (turn["UserId"], turn["CompanyId"], turn["metric_id"]) == (7, 51, 17)
    assert turn["Grounding"] == "ok" and turn["PromptVersion"] == "metric_chat_v1"
    assert store.reports == [{"UserId": 7, "CompanyId": 51, "metric_id": 17, "TurnId": 1,
                              "Source": "MODEL", "Period": "FY2025",
                              "IssueSummary": "Check the interest basis."}]  # fmt: skip
    # The second question carries the first turn as history.
    assert second["turnId"] == 2 and second["conversationId"] == body["conversationId"]
    assert [m["content"] for m in model.seen[1][1:3]] == ["How is it calculated?",
                                                         body["answer"]]  # fmt: skip


def test_failures_have_their_own_status() -> None:
    model = FakeModel(_answer("Coverage was 9.9x.", []),
                      GatewayError("transient", "gave up (429)"))  # fmt: skip
    store = FakeStore()
    with _client(store, model) as c:
        rejected = c.post(URL, json={"message": "q"}).json()
        down = c.post(URL, json={"message": "q"}).json()
        assert c.post("/api/companies/51/metrics/99/chat", json={"message": "q"}
                      ).status_code == 404  # fmt: skip
        assert c.post(URL, json={"message": ""}).status_code == 422
    assert rejected["status"] == "cannot_confirm" and rejected["citations"] == []
    assert down["status"] == "unavailable"
    assert [t["Grounding"] for t in store.turns] == ["rejected", "error"]
    assert store.reports == []


def test_a_prompt_the_package_cannot_read_or_none_is_503() -> None:
    store = FakeStore()
    with _client(store, FakeModel()) as c:
        store.prompt_row = PROMPT.model_copy(update={"min_package_version": "9.9.9"})
        assert c.post(URL, json={"message": "q"}).status_code == 503
        store.prompt_row = None
        assert c.post(URL, json={"message": "q"}).status_code == 503
    assert store.turns == []


def test_questions_are_rate_limited_per_user() -> None:
    model = FakeModel(*[_answer("ok", [])] * 3)
    with _client(FakeStore(), model, questions_per_hour=2) as c:
        assert [c.post(URL, json={"message": "q"}).status_code for _ in range(2)] == [200, 200]
        limited = c.post(URL, json={"message": "q"})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 3000


def test_report_only_on_the_user_s_own_turn() -> None:
    store = FakeStore()
    with _client(store, FakeModel(_answer("ok", []))) as c:
        turn = c.post(URL, json={"message": "q"}).json()["turnId"]
        ok = c.post(f"{URL}/{turn}/report", json={"comment": "Too high", "period": "FY2025"})
        assert c.post(f"{URL}/{turn}/report", json={"period": "2025"}).status_code == 422
        assert c.post(f"{URL}/99/report", json={}).status_code == 404
    assert ok.status_code == 201 and ok.json() == {"reportId": 901}
    assert store.reports[-1]["Source"] == "USER" and store.reports[-1]["Comment"] == "Too high"
    with _client(store, FakeModel(), user_id=8) as c:  # someone else's answer
        assert c.post(f"{URL}/{turn}/report", json={}).status_code == 404


def test_authentication_must_be_wired() -> None:
    app = create_app(store=FakeStore(), client=FakeModel(), settings=ChatSettings())
    with TestClient(app) as c:
        assert c.post(URL, json={"message": "q"}).status_code == 501


# -- the SQL store on SQL Server ------------------------------------------------------------------

MSSQL = os.environ.get("MSSQL_TEST_URL")


@pytest.mark.mssql
@pytest.mark.skipif(not MSSQL, reason="MSSQL_TEST_URL not set")
def test_sql_store_on_sql_server() -> None:
    from uw_pipeline.clients.db import make_engine

    schema = f"uwt_chat_{uuid.uuid4().hex[:8]}"
    engine = make_engine(MSSQL or "", None)

    def run(path: str) -> None:
        sql = (ROOT / "deploy" / "sql" / path).read_text(encoding="utf-8")
        sql = sql.replace("dbo_professional360", schema)
        with engine.begin() as c:
            for batch in re.split(r"^GO\s*$", sql, flags=re.M):
                if batch.strip():
                    c.execute(text(batch))

    run("app_schema_full.sql")
    run("bff_metric_chat.sql")
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO {schema}.Company (CompanyName, NameAbbreviation, CIK) "
                       "VALUES (N'General Motors', N'GM', N'0001467858')"))  # fmt: skip
        c.execute(text(f"INSERT INTO {schema}.metrics (metric_name) VALUES (N'Interest Coverage')"))
        c.execute(text(f"INSERT INTO {schema}.MetricChatContext (CompanyId, metric_id, "
                       "ContextVersion, ContextJson, UpdatedAt) "
                       "VALUES (1, 1, 1, :j, GETUTCDATE())"),
                  {"j": json.dumps(CONTEXT)})  # fmt: skip
        c.execute(text(f"INSERT INTO {schema}.ChatPrompt (Task, PromptVersion, SystemText, "
                       "OutputSchema, ContextVersions, MinPackageVersion, IsActive, UpdatedAt) "
                       "VALUES (N'metric_chat', N'metric_chat_v1', N'RULES', :s, N'1', N'0.1.0', "
                       "1, GETUTCDATE())"), {"s": json.dumps(ANSWER_SCHEMA)})  # fmt: skip
    store = SqlChatStore(engine, schema)
    stored = store.context(1, 1)
    assert stored is not None and json.loads(stored.context)["company"]["ticker"] == "GM"
    assert store.context(1, 2) is None
    prompt = store.prompt()
    assert prompt is not None and prompt.output_schema == ANSWER_SCHEMA
    conv = uuid.uuid4()
    ids = [store.add_turn({"ConversationId": conv, "UserId": 7, "CompanyId": 1, "metric_id": 1,
                           "Question": f"q{i}", "Answer": f"a{i}", "Grounding": "ok"})
           for i in range(3)]  # fmt: skip
    assert ids == sorted(ids)
    assert [t.question for t in store.history(conv, 7, 1, 1, 2)] == ["q1", "q2"]
    assert store.history(conv, 8, 1, 1, 6) == []
    assert store.turn_owner(ids[0]) == TurnOwner(7, 1, 1) and store.turn_owner(10**9) is None
    report = store.add_report({"UserId": 7, "CompanyId": 1, "metric_id": 1, "TurnId": ids[0],
                               "Source": "USER", "Period": "FY2025", "Comment": "x"})  # fmt: skip
    assert report >= 1
