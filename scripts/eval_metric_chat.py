"""Offline evaluation of the metric chat (Phase 19.B, D-091): the questions in
tests/fixtures/metric_chat_eval.yaml against the stored contexts, through the same
`answer_question` the BFF calls.

    python scripts/eval_metric_chat.py --provider securegpt        # work network: the models
                                                                   # in config/metric_chat.yaml
    python scripts/eval_metric_chat.py                             # the pipeline's LLM profile
                                                                   # (LLM_PROVIDER), for drafts
    python scripts/eval_metric_chat.py --only gm-ic-how cat-ic-how --models gpt-6-sol-2026-09-22

Contexts come from the pipeline database (metric_chat_context; run metricdetail first). Each
model answers every case once. Reported per model: grounding passed (shown answer), cases that
met their expectations, latency and tokens; per case the answer and what failed. Report:
data/reports/metric-chat-eval-<date>-<label>.md. Every question is a paid model call.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from uw_metric_chat import (  # noqa: E402
    ANSWER_SCHEMA,
    AsyncSecureGPT,
    ChatPromptRow,
    ChatReply,
    HistoryTurn,
    SecureGPTSettings,
    answer_question,
)
from uw_metric_chat.securegpt import Completion  # noqa: E402
from uw_pipeline.clients.db import pipeline_engine  # noqa: E402
from uw_pipeline.enrich.llm import PROMPTS_DIR  # noqa: E402
from uw_pipeline.enrich.metric_chat_context import CONTEXT_VERSION  # noqa: E402
from uw_pipeline.jobs.run_publish import load_metric_chat_config  # noqa: E402
from uw_pipeline.jobs.stages import StageRunner  # noqa: E402
from uw_pipeline.schema import company, metric_chat_context  # noqa: E402
from uw_pipeline.settings import Settings  # noqa: E402

CASES = ROOT / "tests" / "fixtures" / "metric_chat_eval.yaml"


@dataclass
class Case:
    id: str
    ticker: str
    metric: str
    question: str
    expect: dict[str, Any]
    history: list[HistoryTurn] = field(default_factory=list)


@dataclass
class Result:
    case: Case
    reply: ChatReply | None
    failures: list[str]
    seconds: float


def load_cases(path: Path, only: Sequence[str]) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    cases = [Case(c["id"], c["ticker"], c["metric"], c["question"], c.get("expect") or {},
                  [HistoryTurn(**h) for h in c.get("history") or []]) for c in raw]  # fmt: skip
    return [c for c in cases if not only or c.id in only]


def load_contexts(settings: Settings, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    engine = pipeline_engine(settings)
    x, co = metric_chat_context.c, company.c
    with engine.connect() as conn:
        rows = conn.execute(
            select(co.ticker, x.metric_code, x.context_json)
            .join(company, co.company_id == x.company_id)
            .where(co.ticker.in_(sorted({t for t, _ in wanted})))
        ).all()  # fmt: skip
    return {(t, m): js for t, m, js in rows if (t, m) in wanted}


def judge(case: Case, reply: ChatReply) -> list[str]:
    """What the reply got wrong against the case's expectations."""
    e, out = case.expect, []
    want_scope = e.get("in_scope", True)
    if reply.grounding == "error":
        out.append(f"error: {reply.grounding_detail}")
    elif want_scope and reply.grounding != "ok" and not e.get("allow_rejected"):
        out.append(f"rejected: {reply.grounding_detail}")
    if want_scope is not None and reply.in_scope != want_scope:
        out.append(f"in_scope {reply.in_scope}, expected {want_scope}")
    cited = [c.id for c in reply.citations]
    for pattern in e.get("cites", []):
        if not any(fnmatch.fnmatchcase(c, pattern) for c in cited):
            out.append(f"no citation matching {pattern}")
    if "possible_issue" in e and reply.possible_issue != e["possible_issue"]:
        out.append(f"possible_issue {reply.possible_issue}, expected {e['possible_issue']}")
    text = reply.answer.lower()
    if e.get("mentions_any") and not any(w.lower() in text for w in e["mentions_any"]):
        out.append(f"mentions none of {e['mentions_any']}")
    for w in e.get("not_mentions", []):
        if w.lower() in text:
            out.append(f"mentions {w!r}")
    return out


class PipelineModel:
    """The pipeline's LLM client (its LLM_PROVIDER profile) behind the chat interface: the
    system message as system, earlier turns and the question as one user message."""

    def __init__(self, client: Any, effort: str = "medium") -> None:
        self.client, self.effort = client, effort

    async def complete_json(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        schema: Any,
        max_tokens: int = 1500,
    ) -> Completion:
        system = messages[0]["content"]
        turns = []
        for m in messages[1:-1]:
            who = "Earlier question" if m["role"] == "user" else "Your earlier answer"
            turns.append(f"{who}: {m['content']}")
        user = "\n\n".join([*turns, f"Question: {messages[-1]['content']}"])
        started = time.monotonic()
        result = await asyncio.to_thread(
            self.client.complete_json, model=model, effort=self.effort,
            max_tokens=max(max_tokens, 4000), system=system, user=user, schema=dict(schema),
        )  # fmt: skip
        return Completion(output=result.output, raw=json.dumps(result.output), model=result.model,
                          input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                          latency_ms=int((time.monotonic() - started) * 1000))  # fmt: skip


async def run_model(
    client: Any, model: str, prompt: ChatPromptRow, cases: list[Case],
    contexts: Mapping[tuple[str, str], str], concurrency: int,
) -> list[Result]:  # fmt: skip
    gate = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> Result:
        ctx = contexts.get((case.ticker, case.metric))
        if ctx is None:
            return Result(case, None, ["no stored context (run metricdetail)"], 0.0)
        async with gate:
            started = time.monotonic()
            reply = await answer_question(client, prompt, ctx, case.history, case.question,
                                          model=model)  # fmt: skip
            return Result(case, reply, judge(case, reply), time.monotonic() - started)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def report(by_model: Mapping[str, list[Result]], provider: str) -> str:
    lines = [f"# Metric chat evaluation ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC, {provider})", "",
             "| Model | Cases | Grounded (shown) | Met expectations | Median latency | "
             "Tokens in / out (mean) |", "|---|---|---|---|---|---|"]  # fmt: skip
    for model, results in by_model.items():
        replies = [r.reply for r in results if r.reply]
        shown = sum(1 for r in replies if r.grounding == "ok")
        met = sum(1 for r in results if not r.failures)
        lat = statistics.median([r.seconds for r in results if r.reply] or [0])
        tin = statistics.mean([r.input_tokens or 0 for r in replies] or [0])
        tout = statistics.mean([r.output_tokens or 0 for r in replies] or [0])
        lines.append(f"| {model} | {len(results)} | {shown} | {met} | {lat:.1f} s | "
                     f"{tin:,.0f} / {tout:,.0f} |")  # fmt: skip
    for model, results in by_model.items():
        lines += ["", f"## {model}", ""]
        for r in results:
            mark = "ok" if not r.failures else "FAIL"
            lines.append(f"### {r.case.id} ({r.case.ticker} {r.case.metric}): {mark}")
            lines.append(f"Q: {r.case.question}")
            if r.reply:
                cites = ", ".join(c.id for c in r.reply.citations) or "none"
                flags = (f"{r.reply.grounding}, in_scope={r.reply.in_scope}, "
                         f"possible_issue={r.reply.possible_issue}")  # fmt: skip
                lines.append(f"A ({flags}): {r.reply.answer}")
                lines.append(f"Citations: {cites}")
                if r.reply.issue_summary:
                    lines.append(f"Issue: {r.reply.issue_summary}")
                if r.reply.grounding_detail:
                    lines.append(f"Check: {r.reply.grounding_detail}")
                if r.reply.grounding != "ok" and r.reply.raw_answer:
                    lines.append(f"Model's answer: {r.reply.raw_answer}")
            for f in r.failures:
                lines.append(f"- {f}")
            lines.append("")
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    mcfg = load_metric_chat_config(settings.config_dir)
    prompt = ChatPromptRow(
        prompt_version=mcfg.prompt_version,
        system_text=(PROMPTS_DIR / f"{mcfg.prompt_version}.md").read_text(encoding="utf-8"),
        output_schema=ANSWER_SCHEMA, context_versions=str(CONTEXT_VERSION),
        min_package_version=mcfg.min_package_version,
    )  # fmt: skip
    cases = load_cases(args.cases, args.only)
    contexts = load_contexts(settings, {(c.ticker, c.metric) for c in cases})
    by_model: dict[str, list[Result]] = {}
    if args.provider == "securegpt":
        client = AsyncSecureGPT(SecureGPTSettings.from_env())
        try:
            for model in args.models or mcfg.models:
                by_model[model] = await run_model(client, model, prompt, cases, contexts,
                                                  args.concurrency)  # fmt: skip
        finally:
            await client.aclose()
        label = "securegpt"
    else:
        engine = pipeline_engine(settings)
        with StageRunner(settings, engine) as runner:
            built = runner.llm
            if built is None:
                sys.exit("no LLM client: set LLM_ENABLED and the profile's credentials")
            llm, lcfg = built
            model = (args.models or [lcfg.synthesis.model])[0]
            by_model[model] = await run_model(PipelineModel(llm), model, prompt, cases,
                                              contexts, args.concurrency)  # fmt: skip
        label = settings.llm_provider or "pipeline"
    text = report(by_model, label)
    if args.provider == "pipeline":  # its client caches the system message
        text = text.replace(
            "\n\n|",
            "\n\nInput tokens exclude the cached system message (context and prompt).\n\n|",
            1,
        )
    name = f"metric-chat-eval-{datetime.now(UTC):%Y%m%d-%H%M}-{label}.md"
    out = args.out or (ROOT / "data" / "reports" / name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text.split("\n## ", 1)[0])
    print(f"report: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["pipeline", "securegpt"], default="pipeline")
    ap.add_argument("--models", nargs="*", help="default: config/metric_chat.yaml (securegpt) "
                    "or the profile's synthesis model (pipeline)")  # fmt: skip
    ap.add_argument("--only", nargs="*", default=[], help="case ids")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=Path)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
