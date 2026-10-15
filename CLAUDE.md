# uw-pipeline

Python data pipeline: SEC EDGAR + Alpha Vantage -> metrics/signals -> LLM insights -> Azure SQL,
read by the Professional 360 BFF. The plan of record is `docs/PLAN.md`; decisions not covered
there go in `docs/decisions.md` (record options and rationale before proceeding).

## Commands

```bash
.venv/bin/pip install -e ".[dev]"             # Python 3.14 venv (project supports >=3.11)
.venv/bin/uw-pipeline run --all     # company -> facts -> compute -> documents -> transcripts
                                     #   -> news -> scores -> textmetrics -> signals -> insights
                                     #   -> metricdetail -> publish (app database) -> dq
.venv/bin/uw-pipeline audit-insights # re-verify insight evidence (Phase 5 acceptance)
.venv/bin/uw-pipeline run --job nightly   # scheduled job (config/jobs.yaml); resumes a dead run
.venv/bin/uw-pipeline runs           # recent runs and failed/skipped tasks
.venv/bin/uw-pipeline dq-report      # data-quality findings of the latest run
.venv/bin/uw-pipeline run --ticker MAR --stage compute
.venv/bin/uw-pipeline run --ticker MAR --force       # ignore same-day raw cache / input hash
.venv/bin/uw-pipeline coverage-report                # data/reports/coverage-<date>.md
.venv/bin/uw-pipeline review                          # disputed signals / identity breaks; approve|reject
.venv/bin/python scripts/stability_check.py --faults MAR   # failure handling (--runs 3: live, costs)
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/python scripts/refresh_sec_fixtures.py   # rebuild SEC fixtures from data/raw
.venv/bin/python scripts/securegpt_smoke.py         # SecureGPT gateway check (work network)
.venv/bin/python scripts/runware_smoke.py           # Runware gateway check (LLM_PROVIDER=runware)
.venv/bin/python scripts/azure_smoke.py             # Azure SQL + ADLS check (work network)
scripts/test_mssql.sh --all                          # whole test suite on a local SQL Server (Docker)
.venv/bin/python scripts/export_app_ddl.py           # regenerate deploy/sql/*.sql after schema changes
.venv/bin/python scripts/compare_models.py --model claude-sonnet-5-5 MAR JPM ENPH  # model A/B
```

Architecture, stages, data model and internals: `docs/TECHNICAL.md`.
Operations (adding companies/metrics, prompt changes, troubleshooting): `docs/OPERATIONS.md`.
Pipeline output for the BFF (application tables, `schema_app.py`): `docs/schema-contract.md`.

Local dev: settings come from env vars / `.env` (see `.env.example`). Raw responses land in
`data/raw/` (stand-in for ADLS) and the database defaults to SQLite at `data/uw.db`.

## Conventions

- Before starting each Phase, list the files to change and the plan; wait for confirmation.
- Unit tests must not touch the network (enforced in `tests/conftest.py`); every new external
  call needs a recorded fixture under `tests/fixtures/`.
- XBRL tag mappings, metric formulas and industry-group rules live in `config/`, never in code.
- LLM output is never written to the DB directly; it must pass schema + grounding validation.
- DB writes must be idempotent (natural-key upsert / MERGE). Never DELETE-all-then-insert.
- Do not modify the BFF repo; record contract issues in `docs/schema-contract.md`.
- Secrets come only from environment variables. Never put them in code, logs or fixtures.
- After finishing a checklist item, tick its box in `docs/PLAN.md`.
- All external calls (HTTP, LLM, DB) go through `src/uw_pipeline/clients/` so tests can swap them.
- Companies are identified by pinned CIK in `config/companies.yaml`; tickers are display-only.
- Metric lists per industry group are curated in `config/metrics.yaml` (metric_sets) and
  `config/industry_groups.yaml`; never generate them at run time.
- Every metric gap shown by `coverage-report` needs a note in `config/coverage_notes.yaml`.
- Golden values come from the filing itself (XBRL instance / document), not from estimates.
- LLM: prompts in `src/uw_pipeline/enrich/prompts/{task}_v{N}.md`; any change gets a new
  version. Calls go through `enrich/llm.cached_call` (cached by input hash). Outputs are
  schema-validated and evidence must be grounded in the source text. Tests use
  `tests/fake_llm.py`, never the real API. `LLM_ENABLED=false` runs rules only.
- Every run is tracked in pipeline_run/pipeline_task. A new stage needs an entry in
  `jobs/orchestrator.DEPENDS`, `jobs/stages.StageRunner._stage` and `config/jobs.yaml`.
- Grounding rules changed? Bump `GROUNDING_VERSION` in `jobs/run_insights.py`. Prompt changed?
  New prompt version, then `scripts/export_golden.py` and review `git diff tests/golden`.
- Alpha Vantage: never log or store the API key (messages are masked in `clients/alphavantage.py`).
  The free plan has ~25 calls/day; stages stop cleanly at the quota and resume next run.
- Signal and section rules live in `config/signal_rules.yaml` and `company_overrides/*.yaml`
  (`sections:`); every false positive found becomes a test case in `tests/test_signals.py`.
