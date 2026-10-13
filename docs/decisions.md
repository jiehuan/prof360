# Decision log

## Phase 1 (2026-10-13)

**D-001 Repo layout.** The project lives at the repo root, not in a nested `uw-pipeline/`
directory. The layout under it follows PLAN §4.

**D-002 Python 3.14 runtime, `requires-python >=3.11`.** The local venv uses 3.14 (user's
choice). ruff/mypy target 3.11 so the code stays deployable on the 3.11+ base images the plan
assumes.

**D-003 No Azure SQL or ADLS access yet.**
- Database: SQLAlchemy with a local SQLite file (`DATABASE_URL`, default `sqlite:///data/uw.db`).
  `load/upsert.py` dispatches on dialect: SQLite uses `INSERT … ON CONFLICT DO UPDATE`, and SQL
  Server uses a `#staging` table plus `MERGE` (only the SQL text is unit-tested; it has not run
  against Azure SQL).
- Schema: `metadata.create_all` runs only on SQLite. Who owns migrations for Azure SQL is still
  open (PLAN §12 Q1, deferred).
- Raw landing: `LocalStorage` under `data/raw/{source}/{endpoint}/{id}/{YYYY-MM-DD}.json`. It sits
  behind the `Storage` protocol, so an ADLS implementation can drop in later.

**D-004 Raw cache = one file per resource per UTC fetch date.** Same-day reruns read the landed
file instead of calling EDGAR, and `--force` refetches. This gives replayability and keeps
reruns cheap. A finer content-hash check for incremental work comes with the stages that need it
(companyfacts in Phase 2).

**D-005 Rate limiter.** Token bucket at 8 req/s with capacity 1 (strict 125 ms spacing). A
bucket with capacity 8 could allow up to 16 requests in one second (burst + refill), which
would exceed SEC's 10 req/s limit.

**D-006 Pinned CIKs; tickers are display-only.** SEC's `company_tickers.json` now maps **PARA to
Banzai International (CIK 1826011)**, not Paramount. Following tickers blindly would silently
load the wrong company. Every company in `companies.yaml` pins its CIK. A conflict with the SEC
ticker map logs a warning, and the pinned CIK wins.
- Paramount: the successor registrant is CIK 2041610 (New Pluto Global → Paramount Skydance
  Corp/PSKY → **Skydance Corp/SKYD** since 2026-10-06). The configured ticker is SKYD, and
  `former_tickers: [PARA, PSKY]` still work on the CLI. The front end currently labels this
  company PARA.
- Paramount Global (CIK 813828) filed through mid-2025. It is recorded as `predecessor_ciks` so
  Phase 2 can decide whether to stitch 5-year history from it.

**D-007 MVP scope = one company per SIC.** This answers PLAN §12 Q6. The 9 front-end companies
have 9 distinct SICs.

**D-008 Industry grouping is configured per SIC** (`config/industry_groups.yaml`, answering
PLAN §12 Q4). Lookup order is exact SIC → range fallback → `UNCLASSIFIED` (logged as a
warning). Each group carries `is_financial`, which gates the bank-style FI modules (NIM, CET1,
NPL, loan portfolio). In the fallback ranges, 6000–6299 count as FI (banks, credit, brokers) and
6712 (bank holding companies) counts as FI. **Insurance (6300–6411) is set to
`is_financial: false`** because bank capital and loan metrics don't apply to insurers. Insurers
get their own metric set when one is onboarded. Revisit this if the business wants insurers
treated as FI.

**D-009 `company` table additions vs PLAN §8.** Added `legal_name` (SEC name; `name` is the
display name from config, falling back to the SEC name), `is_financial`, `hq_country`,
`exchange`, `state_of_incorporation`, and lineage fields (`source_url`, `source_hash`,
`raw_path`). `logo_url` is omitted because logos already exist in the BFF (PLAN §12 Q9).
`company_id` is a surrogate key and `cik` is the natural upsert key.

**D-010 pandas** is the dataframe library (PLAN §3 choice). It is first used in Phase 2.
