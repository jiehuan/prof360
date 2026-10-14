"""Model stability and failure check (Jira T2 for SecureGPT acceptance; decisions D-046).

    python scripts/stability_check.py --runs 3 MAR JPM      # live model calls: costs money
    python scripts/stability_check.py --faults MAR           # no model calls

Live mode: on a temporary copy of the pipeline database (SQLite), with the LLM cache emptied
each time, run the textmetrics and governance stages for the companies `--runs` times with the
configured provider (LLM_PROVIDER), then compare what was stored: text metric values, loan book
and board facts. A receipt (models, tokens, timing, differences) goes to
data/reports/stability-<UTC time>.json. Identical results on identical inputs pass.

Fault mode: the same stages with a client that times out, is rate limited, or returns output
that fails the schema. Each must fail closed: no exception escapes, nothing new is stored.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, select

from uw_pipeline.clients.db import ensure_schema
from uw_pipeline.clients.llm import LLMError, LLMResult
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_companies, load_industry_config
from uw_pipeline.enrich.sources import load_source_labels
from uw_pipeline.enrich.text_metrics import load_text_config
from uw_pipeline.jobs.run_governance import run_governance_stage
from uw_pipeline.jobs.run_text_metrics import run_text_metrics_stage
from uw_pipeline.jobs.stages import StageRunner
from uw_pipeline.quality.dq import load_dq_config
from uw_pipeline.schema import board_composition, llm_call, loan_portfolio_segment, metric_value
from uw_pipeline.settings import Settings


def _snapshot(engine: Any) -> dict[str, Any]:
    with engine.connect() as c:
        values = sorted(
            (r.company_id, r.metric_code, r.period_type, r.fiscal_year, r.fiscal_quarter,
             round(r.value, 6) if r.value is not None else None)
            for r in c.execute(select(metric_value).where(
                metric_value.c.derivation.in_(("LLM_EXTRACTED", "PRELIMINARY"))))
        )  # fmt: skip
        loans = sorted((r.company_id, r.period_end, r.segment, r.outstanding, r.nonaccrual)
                       for r in c.execute(select(loan_portfolio_segment)))  # fmt: skip
        board = sorted((r.company_id, r.board_size, r.independent_directors, r.ceo_is_chair,
                        r.lead_independent_director, r.committees_json)
                       for r in c.execute(select(board_composition)))  # fmt: skip
        usage = [dict(r._mapping) for r in c.execute(
            select(llm_call.c.model, llm_call.c.served_model,
                   func.count().label("calls"), func.sum(llm_call.c.input_tokens).label("input"),
                   func.sum(llm_call.c.output_tokens).label("output"))
            .group_by(llm_call.c.model, llm_call.c.served_model))]  # fmt: skip
    return {"values": values, "loans": loans, "board": board, "usage": usage}


def _stages(runner: StageRunner, engine: Any, companies: list[Any], llm: Any) -> list[Any]:
    s = runner.settings
    text = run_text_metrics_stage(
        companies, load_registry(s.config_dir), load_industry_config(s.config_dir),
        load_text_config(s.config_dir), llm, runner.storage, engine,
        labels=load_source_labels(s.config_dir),
        identities=load_dq_config(s.config_dir).metric_identities,
    )  # fmt: skip
    return [*text, *run_governance_stage(companies, llm, runner.storage, engine)]


class _Faulty:
    """A client failing in one of the ways the gateway can."""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def complete_json(self, **_: Any) -> LLMResult:
        if self.mode == "timeout":
            raise LLMError("timed out after retries")
        if self.mode == "rate_limit":
            raise LLMError("429 rate limited after retries")
        return LLMResult(output={"unexpected": True}, model="fault", input_tokens=1,
                         output_tokens=1)  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--faults", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    source_db = settings.database_url.removeprefix("sqlite:///")
    if not settings.database_url.startswith("sqlite:///"):
        print("Needs the SQLite pipeline database (it works on a temporary copy).")
        return 2
    wanted = {t.upper() for t in args.tickers}
    companies = [c for c in load_companies(settings.config_dir) if c.ticker in wanted]
    # Only its storage and LLM client are used; the stages get each copy's own engine.
    runner = StageRunner(settings, create_engine(settings.database_url))
    receipt: dict[str, Any] = {"started": datetime.now(UTC).isoformat(), "tickers": sorted(wanted)}
    with tempfile.TemporaryDirectory() as tmp:
        if args.faults:
            results = {}
            for mode in ("timeout", "rate_limit", "invalid_schema"):
                copy = Path(tmp, f"{mode}.db")
                shutil.copy(source_db, copy)
                engine = create_engine(f"sqlite:///{copy}")
                ensure_schema(engine)
                with engine.begin() as c:
                    c.execute(delete(llm_call))
                before = _snapshot(engine)
                llm = runner.llm
                assert llm is not None, "no LLM profile configured"
                try:
                    res = _stages(runner, engine, companies, (_Faulty(mode), llm[1]))
                    escaped = None
                except Exception as exc:  # must not happen
                    res, escaped = [], repr(exc)
                after = _snapshot(engine)
                same = all(before[k] == after[k] for k in ("values", "loans", "board"))
                tasks = [(r.ticker, r.stage, r.ok, r.detail) for r in res]
                results[mode] = {"exception": escaped, "nothing_new_stored": same, "tasks": tasks}
                print(f"{mode}: exception={escaped} unchanged={same}")
            receipt["faults"] = results
            ok = all(r["exception"] is None and r["nothing_new_stored"] for r in results.values())
        else:
            if runner.llm is None:
                print("No LLM configured (LLM_PROVIDER and its credentials).")
                return 2
            runs = []
            for i in range(args.runs):
                copy = Path(tmp, f"run{i}.db")
                shutil.copy(source_db, copy)
                engine = create_engine(f"sqlite:///{copy}")
                ensure_schema(engine)
                with engine.begin() as c:
                    c.execute(delete(llm_call))  # every run asks the model again
                start = time.monotonic()
                res = _stages(runner, engine, companies, runner.llm)
                snap = _snapshot(engine)
                runs.append({
                    "seconds": round(time.monotonic() - start, 1), "usage": snap["usage"],
                    "tasks": [(r.ticker, r.stage, r.ok, r.detail) for r in res],
                    "result": {k: snap[k] for k in ("values", "loans", "board")},
                })  # fmt: skip
                print(f"run {i + 1}: {runs[-1]['seconds']}s, {snap['usage']}")
            first = runs[0]["result"]
            diffs = {f"run {i + 1}": {k: sorted(set(map(repr, r["result"][k]))
                                                ^ set(map(repr, first[k])))[:20]
                                      for k in first if r["result"][k] != first[k]}
                     for i, r in enumerate(runs[1:], start=1)}  # fmt: skip
            receipt |= {"provider": runner.llm[1].provider, "runs": runs,
                        "differences": {k: v for k, v in diffs.items() if v}}  # fmt: skip
            ok = not receipt["differences"]
            print("identical across runs" if ok else f"DIFFERENT: {receipt['differences']}")
    out = Path(settings.data_dir, "reports",
               f"stability-{datetime.now(UTC):%Y%m%dT%H%M%S}.json")  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1, default=str), encoding="utf-8")
    print(f"receipt: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
