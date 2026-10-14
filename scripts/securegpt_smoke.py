"""Smoke test for the SecureGPT gateway before switching the pipeline to it (D-039).

Run from a machine that can reach the gateway, with credentials in the environment:

    export SECUREGPT_CLIENT_ID=...  SECUREGPT_CLIENT_SECRET=...  SECUREGPT_SUBSCRIPTION_KEY=...
    export SECUREGPT_ENV=dv          # dv | pp | pr
    python scripts/securegpt_smoke.py

For each model in config/llm.yaml and insights.yaml it makes one small structured-output call
with a real pipeline schema (nested objects, enum, $ref), once without and once with
`reasoning_effort`, and prints the result. Credentials are never printed. Nothing is written
to the database. Cost: a few cents.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from uw_pipeline.clients.llm import LLMError, LLMSetupError
from uw_pipeline.clients.securegpt import SecureGPTClient, load_securegpt_config
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.enrich.news_relevance import Verdicts
from uw_pipeline.enrich.package import load_insights_config
from uw_pipeline.enrich.schemas import strict_json_schema

CONFIG = Path(__file__).resolve().parents[1] / "config"
SYSTEM = ("You screen news for underwriters. For each article give a verdict: SUBJECT if the "
          "company is the main subject, otherwise PASSING. Use the ids as given.")  # fmt: skip
USER = ("Company: Marriott International\n\nid | headline | summary\n"
        "N1 | Marriott raises full-year RevPAR outlook | The hotel group beat estimates.\n"
        "N2 | Hotel stocks to watch this week | A roundup of lodging names.")  # fmt: skip


def main() -> int:
    missing = [v for v in ("SECUREGPT_CLIENT_ID", "SECUREGPT_CLIENT_SECRET",
                           "SECUREGPT_SUBSCRIPTION_KEY") if not os.environ.get(v)]  # fmt: skip
    if missing:
        print(f"set {', '.join(missing)} first")
        return 2
    cfg = load_securegpt_config(CONFIG)
    env = os.environ.get("SECUREGPT_ENV", cfg.default_env)
    llm, ins = load_llm_config(CONFIG), load_insights_config(CONFIG)
    models = sorted({llm.extractor.model, llm.tiebreaker.model, ins.synthesis.model})
    schema = strict_json_schema(Verdicts)
    print(f"environment {env}, api-version {cfg.api_version}, models {models}\n")
    ok = True
    effort_ok = True
    for send_effort in (False, True):
        client = SecureGPTClient(
            cfg.model_copy(update={"send_reasoning_effort": send_effort}), env=env,
            client_id=os.environ["SECUREGPT_CLIENT_ID"],
            client_secret=os.environ["SECUREGPT_CLIENT_SECRET"],
            subscription_key=os.environ["SECUREGPT_SUBSCRIPTION_KEY"],
        )  # fmt: skip
        for model in models:
            label = f"{model:<24} reasoning_effort={'on ' if send_effort else 'off'}"
            start = time.monotonic()
            try:
                res = client.complete_json(model=model, effort="low", max_tokens=4000,
                                           system=SYSTEM, user=USER, schema=schema)  # fmt: skip
                verdicts = {a["id"]: a["verdict"] for a in res.output.get("articles", [])}
                good = verdicts == {"N1": "SUBJECT", "N2": "PASSING"}
                print(f"OK    {label} {time.monotonic() - start:5.1f}s "
                      f"in={res.input_tokens} out={res.output_tokens} served={res.model} "
                      f"answer={'as expected' if good else verdicts}")  # fmt: skip
            except (LLMError, LLMSetupError) as exc:
                print(f"FAIL  {label} {type(exc).__name__}: {exc}")
                if send_effort:
                    effort_ok = False
                else:
                    ok = False
        client.close()
    print()
    if not ok:
        print("Basic calls failed: check credentials, SECUREGPT_ENV, network and deployments.")
        return 1
    print("Basic calls work.")
    print("reasoning_effort: " + ("accepted -> set send_reasoning_effort: true in "
                                  "config/securegpt.yaml" if effort_ok else
                                  "rejected -> keep send_reasoning_effort: false"))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
