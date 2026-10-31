"""Phase 17d (D-062): a company's independent model calls asked a few at a time before the
stage's sequential code, which then finds every answer in the cache.

Model answers come from tests/fake_llm.py; the text-metric input is the release stand-in of
tests/test_short_transactions.py.
"""

import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from tests.test_llm_cache import Out
from tests.test_short_transactions import IND, REG, _company, _doc, _html, _snapshot
from uw_pipeline.clients.llm import LLMError
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.llm import ModelSpec, Request, cached_call, load_llm_config, prefetch
from uw_pipeline.enrich.text_metrics import load_text_config
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.schema import llm_call, metadata, metric_value

NOW = datetime(2026, 10, 21, tzinfo=UTC)
SPEC = ModelSpec(model="m", effort="low", max_tokens=100)


def _req(user: str) -> Request:
    return Request(SPEC, "t", "v1", "s", user, {}, Out.model_validate)


def _call(conn, llm, user):
    return cached_call(conn, llm, SPEC, task="t", prompt_version="v1", system="s", user=user,
                       schema={}, now=NOW, validate=Out.model_validate)  # fmt: skip


class Overlap:
    """A fake model whose calls take a while; records how many ran at once."""

    def __init__(self, answer) -> None:
        self.lock, self.now, self.peak, self.answer = threading.Lock(), 0, 0, answer

    def __call__(self, model, user):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(0.1)
        with self.lock:
            self.now -= 1
        return self.answer(model, user)


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        yield c


def test_prefetch_asks_together_and_the_calls_find_the_answers(conn) -> None:
    def answer(model, user):
        if user == "fails":
            return LLMError("overloaded")
        return {"n": "not a number"} if user == "invalid" else {"n": len(user)}

    overlap = Overlap(answer)
    llm = FakeLLM(overlap)
    assert _call(conn, llm, "cached") == {"n": 6}
    users = ["aa", "bbb", "aa", "cached", "fails", "invalid"]
    stored = prefetch(conn, llm, [_req(u) for u in users], now=NOW, workers=3)
    # Asked: aa, bbb, fails, invalid (aa once; cached not again), three at a time.
    assert (stored, len(llm.calls), overlap.peak) == (2, 5, 3)
    rows = {r.input_hash: r for r in conn.execute(select(llm_call))}
    assert len(rows) == 4  # cached, aa, bbb, invalid (kept for its tokens, not as an answer)
    assert sum(r.output_json is None for r in rows.values()) == 1
    # The calls that follow: answers from the cache; the failed and the invalid one ask again.
    assert [_call(conn, llm, u) for u in ("aa", "bbb", "cached")] == [{"n": 2}, {"n": 3},
                                                                      {"n": 6}]  # fmt: skip
    assert len(llm.calls) == 5
    assert _call(conn, llm, "fails") is None and _call(conn, llm, "invalid") is None
    assert len(llm.calls) == 7


def test_one_request_or_one_worker_is_left_to_the_calls(conn) -> None:
    llm = FakeLLM(lambda m, u: {"n": 1})
    assert prefetch(conn, llm, [_req("a")], now=NOW, workers=3) == 0
    assert prefetch(conn, llm, [_req("a"), _req("b")], now=NOW, workers=1) == 0
    assert llm.calls == []


QUARTERS = {"first": ("2026-01-01", "2026-03-31"), "second": ("2026-04-01", "2026-06-30"),
            "third": ("2026-07-01", "2026-09-30")}  # fmt: skip


def _release_answer(model, user):
    """Each release reports its own quarter's revenue."""
    if "Metrics to read" in user:
        return {"values": []}
    quarter = next(q for q in QUARTERS if f"in the {q} quarter" in user)
    start, end = QUARTERS[quarter]
    return {"values": [
        {"metric_code": "revenue", "value": "7,300", "unit": "usd", "scale": "millions",
         "period_start": start, "period_end": end, "period": "quarter",
         "evidence": f"Total revenues were $7,300 million in the {quarter} quarter."}]}  # fmt: skip


def test_textmetrics_reads_together_with_the_same_result(tmp_path) -> None:
    """Three releases: their reads go three at a time, and the values stored are those of one
    call at a time."""
    results = {}
    for workers in (1, 3):
        engine = create_engine(f"sqlite:///{tmp_path / f'w{workers}.db'}")
        metadata.create_all(engine)
        storage = LocalStorage(tmp_path / f"raw{workers}")
        with engine.begin() as c:
            _company(c, 1, "HOTELS")
            for n, quarter in enumerate(QUARTERS, start=1):
                storage.write_bytes(f"ex{n}.htm", _html(
                    f"Company Reports {quarter.title()} Quarter 2026 Results\n"
                    f"Total revenues were $7,300 million in the {quarter} quarter."))  # fmt: skip
                _doc(c, 1, n, "8-K", f"2026-{3 * n + 1:02d}-28", doc_role="EX-99.1",
                     raw_path=f"ex{n}.htm")  # fmt: skip
        overlap = Overlap(_release_answer)
        llm = FakeLLM(overlap)
        cfg = load_llm_config(CONFIG_DIR).model_copy(update={"company_calls": workers})
        [res] = run_text_metrics_stage([CompanyConfig(ticker="CO1", cik=1)], REG, IND,
                                       load_text_config(CONFIG_DIR), (llm, cfg), storage,
                                       engine, now=lambda: NOW)  # fmt: skip
        assert res.ok, res
        results[workers] = (_snapshot(engine, (metric_value,)), overlap.peak, len(llm.calls))
    (one, peak1, calls1), (three, peak3, calls3) = results[1], results[3]
    assert one == three and len(one["metric_value"]) == 3
    assert (peak1, peak3, calls1) == (1, 3, calls3)
