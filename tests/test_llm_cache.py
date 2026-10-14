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
