"""Phase 17d (D-061): the LLM stages in short transactions. For each stage, two companies run
in parallel threads on a SQLite file, their model calls overlapping, and the stored result is
the same as running them one after the other.

Inputs are those of each stage's own tests (recorded filing excerpts, fake model answers from
tests/fake_llm.py). The news stage is left out: it runs one company at a time (Alpha Vantage's
quota) and its writes are checked in test_news.
"""

import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Integer, Table, insert, select

from tests import test_clinical, test_governance, test_industry_choice, test_legal, test_scores
from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM, signals
from uw_pipeline.clients.db import make_engine
from uw_pipeline.clients.storage import LocalStorage
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.text_metrics import load_text_config
from uw_pipeline.jobs.run_clinical import run_clinical_stage
from uw_pipeline.jobs.run_governance import run_governance_stage
from uw_pipeline.jobs.run_industry import run_industry_stage
from uw_pipeline.jobs.run_legal import run_legal_stage
from uw_pipeline.jobs.run_scores import load_scores_config, run_scores_stage
from uw_pipeline.jobs.run_signals import run_signals_stage
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.schema import (
    activity,
    board_composition,
    clinical_program,
    company,
    filing_section,
    industry_choice,
    legal_matter,
    llm_call,
    metadata,
    metric_value,
    news_risk_score,
    section_chunk,
    signal,
    signal_vote,
    source_document,
    transcript_flag,
    transcript_tone,
)
from uw_pipeline.signals.rules import load_signal_rules

NOW = datetime(2026, 10, 21, tzinfo=UTC)
LLM_CFG = load_llm_config(CONFIG_DIR)
REG = load_registry(CONFIG_DIR)
IND = load_industry_config(CONFIG_DIR)
CALL_SECONDS = 0.15


def _company(c, cid: int, group: str, sic: str | None = None) -> None:
    c.execute(insert(company).values(
        company_id=cid, cik=f"{cid:010d}", ticker=f"CO{cid}", name=f"Company {cid}",
        legal_name=f"COMPANY {cid} INC.", sic_code=sic, industry_group=group, is_financial=False,
        is_active=True, fiscal_year_end="1231", source_url="u", source_hash="h", raw_path="p",
        last_refreshed_at=NOW))  # fmt: skip


def _doc(c, cid: int, n: int, form: str, filed: str, **over: Any) -> int:
    doc_id = cid * 10 + n
    c.execute(insert(source_document).values(**({
        "doc_id": doc_id, "company_id": cid, "source_type": "SEC_FILING", "form_type": form,
        "doc_role": "PRIMARY", "accession_no": f"{cid:010d}-26-{n:06d}", "doc_name": f"d{doc_id}",
        "filed_at": filed, "url": "u", "content_hash": "h",
    } | over)))  # fmt: skip
    return doc_id


def _html(text: str) -> bytes:
    body = "".join(f"<p>{line}</p>" for line in text.splitlines())
    return f"<html><body>{body}</body></html>".encode("ascii", "xmlcharrefreplace")


# -- each stage: seed one company, its fake answers, its run, the tables it writes ------------


@dataclass(frozen=True)
class Case:
    seed: Callable[[Any, LocalStorage, int], None]
    answer: Callable[[str, str], Any]
    run: Callable[[list[CompanyConfig], Any, LocalStorage, FakeLLM], list[Any]]
    tables: tuple[Table, ...]
    group: str = "RETAIL"
    traits: tuple[str, ...] = ()


def _seed_governance(c, storage, cid):
    storage.write_bytes(f"proxy{cid}.htm", test_governance.PROXY)
    _doc(c, cid, 1, "DEF 14A", "2026-04-01", raw_path=f"proxy{cid}.htm")


def _seed_legal(c, storage, cid):
    storage.write_bytes(f"tenk{cid}.htm", _html(test_legal.EXCERPT))
    _doc(c, cid, 1, "10-K", "2026-02-17", raw_path=f"tenk{cid}.htm")


def _legal_answer(model, user):
    read = test_legal.read
    return {"matters": [read("Hayes v. Enphase Energy, Inc.", test_legal.HAYES,
                             started="July 15, 2024"),
                        read("Pension Fund Action", test_legal.PENSION,
                             started="December 13, 2024")]}  # fmt: skip


def _seed_clinical(c, storage, cid):
    storage.write_bytes(f"tenk{cid}.htm", _html(test_clinical.EXCERPT))
    _doc(c, cid, 1, "10-K", "2026-02-17", raw_path=f"tenk{cid}.htm")


def _seed_industry(c, storage, cid):
    doc_id = _doc(c, cid, 1, "10-K", "2026-02-17")
    text = test_industry_choice.ITEM1
    c.execute(insert(filing_section).values(
        doc_id=doc_id, section_code="10K-1", start_offset=0, char_count=len(text), text=text,
        text_hash="h"))  # fmt: skip


def _seed_scores(c, storage, cid):
    act = {"company_id": cid, "source_name": "Reuters", "source_count": 1,
           "first_seen_at": NOW, "updated_at": NOW}  # fmt: skip
    c.execute(insert(activity), [
        act | {"activity_id": cid * 10 + i, "title": title, "published_at": datetime(2026, 10, i),
               "url": f"u{cid}{i}", "url_hash": f"h{cid}{i}", "av_summary": summary}
        for i, (title, summary) in enumerate([
            ("Sued over data breach", "A class action seeks $100 million."),
            ("Opens hotel in Lisbon", "A new property.")], start=1)
    ])  # fmt: skip
    doc_id = _doc(c, cid, 9, "EARNINGS_CALL", "2026-10-13", source_type="TRANSCRIPT",
                  accession_no=f"AV-CO{cid}-2026Q2", period_end="2026-06-30")  # fmt: skip
    text = "\n".join(test_scores.CHUNKS)
    c.execute(insert(filing_section).values(
        section_id=doc_id, doc_id=doc_id, section_code="TRANSCRIPT", start_offset=0,
        char_count=len(text), text=text, text_hash="th"))  # fmt: skip
    c.execute(insert(section_chunk), [
        {"section_id": doc_id, "chunk_index": i, "start_offset": 0, "end_offset": len(t),
         "text": t} for i, t in enumerate(test_scores.CHUNKS)
    ])  # fmt: skip


def _scores_answer(model, user):
    if "id | date | headline" not in user:
        return test_scores.answer(model, user)  # transcript chunks
    return {"articles": [
        {"id": f"A{aid}", "litigation": 1.0, "regulatory": 0.0, "financial_distress": 0.0,
         "governance": 0.0, "operational": 0.0, "overall": 0.5, "rationale": "A lawsuit."}
        for aid in re.findall(r"^A(\d+) \|", user, re.M)
    ]}  # fmt: skip


RELEASE = ("Company Reports Third Quarter 2026 Results\n"
           "Total revenues were $7,300 million in the third quarter.")  # fmt: skip


def _seed_textmetrics(c, storage, cid):
    storage.write_bytes(f"ex{cid}.htm", _html(RELEASE))
    _doc(c, cid, 1, "8-K", "2026-11-04", doc_role="EX-99.1", raw_path=f"ex{cid}.htm")


def _textmetrics_answer(model, user):
    if "Metrics to read" in user:  # table reads: nothing in this release
        return {"values": []}
    return {"values": [
        {"metric_code": "revenue", "value": "7,300", "unit": "usd", "scale": "millions",
         "period_start": "2026-07-01", "period_end": "2026-09-30", "period": "quarter",
         "evidence": "Total revenues were $7,300 million in the third quarter."}]}  # fmt: skip


WEAKNESS = ("Item 4. Controls and Procedures\nManagement identified a material weakness in "
            "internal control over financial reporting.")  # fmt: skip


def _seed_signals(c, storage, cid):
    doc_id = _doc(c, cid, 1, "10-Q", "2026-08-05", period_end="2026-06-30")
    c.execute(insert(filing_section).values(
        doc_id=doc_id, section_code="10Q-I-4", start_offset=0, char_count=len(WEAKNESS),
        text=WEAKNESS, text_hash="h"))  # fmt: skip


def _llm(llm):
    return (llm, LLM_CFG)


CASES = {
    "governance": Case(
        _seed_governance, lambda m, u: test_governance._facts(),
        lambda cs, e, s, llm: run_governance_stage(cs, _llm(llm), s, e, now=lambda: NOW),
        (board_composition,)),
    "legal": Case(
        _seed_legal, _legal_answer,
        lambda cs, e, s, llm: run_legal_stage(cs, _llm(llm), s, e, now=lambda: NOW),
        (legal_matter,)),
    "clinical": Case(
        _seed_clinical, lambda m, u: {"programs": test_clinical.ALL},
        lambda cs, e, s, llm: run_clinical_stage(cs, _llm(llm), s, e, now=lambda: NOW),
        (clinical_program,), group="PHARMA", traits=("clinical_stage",)),
    "industry": Case(
        _seed_industry, lambda m, u: test_industry_choice.answer("MINING"),
        lambda cs, e, s, llm: run_industry_stage(cs, _llm(llm), REG, IND, e, lambda _: [],
                                                 now=lambda: NOW),
        (industry_choice, company), group="UNCLASSIFIED"),
    "scores": Case(
        _seed_scores, _scores_answer,
        lambda cs, e, s, llm: run_scores_stage(cs, _llm(llm), load_scores_config(CONFIG_DIR),
                                               e, now=lambda: NOW),
        (news_risk_score, transcript_tone, transcript_flag)),
    "textmetrics": Case(
        _seed_textmetrics, _textmetrics_answer,
        lambda cs, e, s, llm: run_text_metrics_stage(cs, REG, IND, load_text_config(CONFIG_DIR),
                                                     _llm(llm), s, e, now=lambda: NOW),
        (metric_value,), group="HOTELS"),
    "signals": Case(
        _seed_signals,
        lambda m, u: signals(findings=[("material_weakness", "identified a material weakness")]),
        lambda cs, e, s, llm: run_signals_stage(cs, None, load_signal_rules(CONFIG_DIR), e,
                                                llm=_llm(llm), now=lambda: NOW),
        (signal, signal_vote)),
}  # fmt: skip


def _snapshot(engine, tables: tuple[Table, ...]) -> dict[str, list[tuple]]:
    """Each table's rows without surrogate ids (they follow insert order) and timestamps."""
    out = {}
    with engine.connect() as c:
        for t in [*tables, llm_call]:
            pk = list(t.primary_key.columns)
            drop = {col.name for col in pk if len(pk) == 1 and isinstance(col.type, Integer)
                    and col.name not in ("company_id", "doc_id")}  # fmt: skip
            drop |= {"created_at", "updated_at", "detected_at", "first_seen_at", "computed_at"}
            cols = [col for col in t.columns if col.name not in drop]
            out[t.name] = sorted(tuple(map(repr, r)) for r in c.execute(select(*cols)))
    return out


def _setup(tmp_path, name: str, case: Case):
    engine = make_engine(f"sqlite:///{tmp_path / name}.db")
    metadata.create_all(engine)
    storage = LocalStorage(tmp_path / f"{name}-raw")
    with engine.begin() as c:
        for cid in (1, 2):
            _company(c, cid, case.group, "6795" if case.group == "UNCLASSIFIED" else None)
            case.seed(c, storage, cid)
    companies = [CompanyConfig(ticker=f"CO{cid}", cik=cid, traits=list(case.traits))
                 for cid in (1, 2)]  # fmt: skip
    return engine, storage, companies


@pytest.mark.parametrize("stage", sorted(CASES))
def test_parallel_matches_serial(tmp_path, stage) -> None:
    case = CASES[stage]
    lock = threading.Lock()
    state: dict[str, Any] = {"now": 0, "peak": 0, "locked": 0, "db": None}

    def slow(model, user):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        # While the model answers, nobody holds the write lock: another writer gets it at once
        # (a write group of another company may finish first; it takes milliseconds).
        probe = sqlite3.connect(state["db"], timeout=0.5, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        except sqlite3.OperationalError:
            with lock:
                state["locked"] += 1
        finally:
            probe.close()
        time.sleep(CALL_SECONDS)
        with lock:
            state["now"] -= 1
        return case.answer(model, user)

    engine, storage, companies = _setup(tmp_path, "serial", case)
    state["db"] = engine.url.database
    serial = case.run(companies, engine, storage, FakeLLM(slow))
    assert all(r.ok for r in serial), serial
    assert (state["peak"], state["locked"]) == (1, 0)
    expected = _snapshot(engine, case.tables)
    assert any(expected[t.name] for t in case.tables), "the stage wrote nothing"

    engine, storage, companies = _setup(tmp_path, "parallel", case)
    state["db"], state["peak"] = engine.url.database, 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: case.run([c], engine, storage, FakeLLM(slow))[0],
                                companies))  # fmt: skip
    assert all(r.ok for r in results), results
    assert (state["peak"], state["locked"]) == (2, 0)
    assert _snapshot(engine, case.tables) == expected
