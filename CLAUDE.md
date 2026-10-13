# uw-pipeline

Python data pipeline: SEC EDGAR + Alpha Vantage -> metrics/signals -> LLM insights -> Azure SQL,
read by the Professional 360 BFF. The plan of record is `docs/PLAN.md`; decisions not covered
there go in `docs/decisions.md` (record options and rationale before proceeding).

## Commands

```bash
.venv/bin/pip install -e ".[dev]"             # Python 3.14 venv (project supports >=3.11)
.venv/bin/uw-pipeline run --all                      # stages: company -> facts -> compute
.venv/bin/uw-pipeline run --ticker MAR --stage compute
.venv/bin/uw-pipeline run --ticker MAR --force       # ignore same-day raw cache / input hash
.venv/bin/uw-pipeline coverage-report                # data/reports/coverage-<date>.md
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/python scripts/refresh_sec_fixtures.py   # rebuild SEC fixtures from data/raw
```

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
