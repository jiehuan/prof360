"""cached_call (review 2026-10-14): invalid output is not reused; a too-large request concerns
one item, not the whole run."""

from datetime import UTC, datetime

import anthropic
import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, select

from tests.fake_llm import FakeLLM
from uw_pipeline.enrich.llm import LLMUnavailableError, ModelSpec, cached_call
from uw_pipeline.schema import llm_call, metadata

NOW = datetime(2026, 10, 14, tzinfo=UTC)
SPEC = ModelSpec(model="m", effort="low", max_tokens=100)


class Out(BaseModel):
    n: int


def _call(conn, llm, user="u"):
    return cached_call(conn, llm, SPEC, task="t", prompt_version="v1", system="s", user=user,
                       schema={}, now=NOW, validate=Out.model_validate)  # fmt: skip


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        yield c


def test_invalid_output_is_retried_not_reused(conn) -> None:
    answers = iter([{"n": "not a number"}, {"n": 3}])
    llm = FakeLLM(lambda model, user: next(answers))
    assert _call(conn, llm) is None
    row = conn.execute(select(llm_call)).mappings().one()
    assert row["output_json"] is None and "validation" in row["error"]
    assert _call(conn, llm) == {"n": 3}  # asked again, not served from the cache
    assert _call(conn, llm) == {"n": 3} and len(llm.calls) == 2  # now cached


def test_request_too_large_fails_the_item_only(conn) -> None:
    def too_long(model, user):
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.example"))
        return anthropic.BadRequestError("prompt is too long: 1200000 tokens > 1000000 maximum",
                                         response=response, body=None)  # fmt: skip

    assert _call(conn, FakeLLM(too_long)) is None  # recorded; other items go on

    def bad_setup(model, user):
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.example"))
        return anthropic.BadRequestError("unknown model 'm'", response=response, body=None)

    with pytest.raises(LLMUnavailableError):
        _call(conn, FakeLLM(bad_setup), user="other")


def test_short_transactions_hold_no_lock_while_the_model_answers(tmp_path) -> None:
    """D-052: with `commit`, the stage's finished writes are committed before the model is
    called, so another worker can write meanwhile (SQLite allows one writer at a time). Without
    it the stage's transaction would hold the lock for the whole call."""
    import sqlite3

    from uw_pipeline.clients.db import make_engine
    from uw_pipeline.schema import llm_task_stat

    path = tmp_path / "w.db"
    engine = make_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    seen: list[str] = []

    def answer(model, user):
        other = sqlite3.connect(path, timeout=0.2)  # another worker's write during the call
        try:
            other.execute("INSERT INTO llm_task_stat (company_id, task, generated, kept, "
                          "created_at) VALUES (2, 'x', 0, 0, '2026-10-14')")  # fmt: skip
            other.commit()
            seen.append("written")
        except sqlite3.OperationalError as exc:
            seen.append(str(exc))
        finally:
            other.close()
        return {"n": 1}

    for commit in (True, False):
        with engine.connect() as conn:
            conn.execute(llm_task_stat.insert().values(company_id=1, task="t", generated=1,
                                                       kept=1, created_at=NOW))  # fmt: skip
            cached_call(conn, FakeLLM(answer), SPEC, task="t", prompt_version="v1", system="s",
                        user=f"u-{commit}", schema={}, now=NOW, commit=commit)  # fmt: skip
            conn.rollback()
    assert seen == ["written", "database is locked"]
