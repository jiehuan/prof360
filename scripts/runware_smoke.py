"""Smoke test for the Runware gateway before using the `runware` profile (D-047).

    python scripts/runware_smoke.py            # RUNWARE_API_KEY from the environment or .env
    python scripts/runware_smoke.py --record   # also save responses as test fixtures

For each model of the runware profile (config/llm.yaml) it makes one small call with a real
pipeline schema in each output mode (strict JSON schema, JSON mode) and with `reasoning_effort`,
and prints what worked, tokens, the reported cost and the answer. The key is never printed.
Nothing is written to the database. `--record` saves the JSON-schema responses (without ids) to
tests/fixtures/runware/. Cost: well under a cent per call. The account needs at least $5 credit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from uw_pipeline.clients.llm import LLMError, LLMSetupError
from uw_pipeline.clients.runware import RunwareClient, load_runware_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.news_relevance import Verdicts
from uw_pipeline.enrich.schemas import strict_json_schema
from uw_pipeline.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
FIXTURES = ROOT / "tests" / "fixtures" / "runware"
SYSTEM = ("You screen news for underwriters. For each article give a verdict: SUBJECT if the "
          "company is the main subject, otherwise PASSING. Use the ids as given.")  # fmt: skip
USER = ("Company: Marriott International\n\nid | headline | summary\n"
        "N1 | Marriott raises full-year RevPAR outlook | The hotel group beat estimates.\n"
        "N2 | Hotel stocks to watch this week | A roundup of lodging names.")  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--profile", default="runware", help="profile in config/llm.yaml")
    args = ap.parse_args()
    settings = Settings()
    if settings.runware_api_key is None:
        print("set RUNWARE_API_KEY first (environment or .env)")
        return 2
    cfg = load_runware_config(CONFIG)
    llm = load_llm_config(CONFIG, args.profile)
    models = list(dict.fromkeys([llm.extractor.model, llm.tiebreaker.model, llm.synthesis.model]))
    schema = strict_json_schema(Verdicts)
    last: dict[str, Any] = {}

    def keep(r: httpx.Response) -> None:  # the raw body, for --record and the reported cost
        r.read()
        last["body"] = r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else None  # fmt: skip

    http = httpx.Client(timeout=cfg.timeout_sec, event_hooks={"response": [keep]})
    results: dict[str, dict[str, bool]] = {}
    for model in models:
        for mode, effort in (("json_schema", False), ("json_schema", True), ("json_object", False)):
            run_cfg = cfg.model_copy(
                update={
                    "output": {model: mode},
                    "max_attempts": 2,
                    "reasoning_effort_models": [model] if effort else [],
                }
            )
            client = RunwareClient(run_cfg, api_key=settings.runware_api_key.get_secret_value(),
                                   http=http)  # fmt: skip
            label = f"{model:<28} {mode:<11} effort={'on ' if effort else 'off'}"
            start = time.monotonic()
            last.clear()
            try:
                res = client.complete_json(model=model, effort="low", max_tokens=4000,
                                           system=SYSTEM, user=USER, schema=schema)  # fmt: skip
                Verdicts.model_validate(res.output)
                verdicts = {a["id"]: a["verdict"] for a in res.output.get("articles", [])}
                good = verdicts == {"N1": "SUBJECT", "N2": "PASSING"}
                cost = ((last.get("body") or {}).get("usage") or {}).get("cost")
                print(f"OK    {label} {time.monotonic() - start:5.1f}s in={res.input_tokens} "
                      f"out={res.output_tokens} cost={cost} served={res.model} "
                      f"answer={'as expected' if good else verdicts}")  # fmt: skip
                results.setdefault(model, {})[f"{mode}{'+effort' if effort else ''}"] = True
                if args.record and mode == "json_schema" and not effort and last.get("body"):
                    FIXTURES.mkdir(parents=True, exist_ok=True)
                    body = {k: v for k, v in last["body"].items() if k not in ("id", "created")}
                    name = model.replace(":", "_").replace("@", "_") + ".json"
                    (FIXTURES / name).write_text(json.dumps(body, indent=1) + "\n")
            except (LLMError, LLMSetupError, ValueError) as exc:
                print(f"FAIL  {label} {type(exc).__name__}: {str(exc)[:200]}")
                results.setdefault(model, {})[f"{mode}{'+effort' if effort else ''}"] = False
    http.close()
    print()
    for model, r in results.items():
        if r.get("json_schema"):
            mode = "json_schema"
        elif r.get("json_object"):
            mode = "json_object  -> add to `output:` in config/runware.yaml"
        else:
            mode = "neither worked"
        note = " ; reasoning_effort accepted -> add to reasoning_effort_models" if r.get(
            "json_schema+effort") else ""  # fmt: skip
        print(f"{model}: {mode}{note}")
    return 0 if all(r.get("json_schema") or r.get("json_object") for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
