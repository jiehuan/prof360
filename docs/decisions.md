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

## Phase 2 (2026-10-13)

**D-011 Tags are picked per period, not per company.** PLAN §5.2 #8 says to use the first
candidate tag that has data. Companies switch tags over time (MAR interest expense moved to
`InterestExpenseNonoperating` in 2024; LUV revenue changed tags after 2020). Picking one tag
for all history would drop recent periods, so each period takes the first candidate with a
value, and the tag used is stored in `metric_value.source_tags`.
- When a company switches tags between its 10-Qs and 10-K (MAR capex in FY2025), Q4 is
  derived at concept level as FY(tag A) − 9M YTD(tag B). Both tags are recorded on the value.

**D-012 SKYD uses successor-only history** (user decision). The successor's own filings also
contain predecessor (Paramount Global) columns: FY2023/24 and Jan–Aug 2025. `companies.yaml`
sets `history_start: 2025-08-08`, and compute drops facts whose period starts before it.
Those facts stay in `xbrl_fact` for audit. SKYD's history therefore starts with the stub
period; YoY growth first becomes available in FY2026 Q3.

**D-013 companyfacts lags filings; recent filings are also read from their own XBRL
instance.** SEC's companyfacts API was still missing Ford's Q2 2026 10-Q (filed 2026-07-29)
on 2026-10-13. ENPH and SKYD also had missing filings. The facts stage lists periodic filings
from submissions. Any filing from the past 400 days that is absent from companyfacts is parsed
from its `*_htm.xml` instance. This is required for the overnight-update requirement (D-022).

**D-014 Company extension and dimensional facts come from filing instances.** companyfacts
contains no dimensional facts and no company taxonomies. Marriott's fee revenues are
dimensional (`srt:ProductOrServiceAxis = mar:FeeServiceMember`), and its D&A uses
`mar:DepreciationAmortizationAndOther`. Concept candidates can name any `prefix:Tag` plus
`dims`. Those candidates are read from every 10-K/10-Q instance within `XBRL_HISTORY_YEARS`
(default 6). Marriott spelled the gross-fee tag `RevenuesGrossofCapitalized…` before 2023;
both spellings are candidates.

**D-015 `xbrl_fact` holds only the tags the registry references**, not all ~600 per company.
The full companyfacts JSON and the instance files stay in raw storage for replay. Rows are
keyed by `fact_key` (sha1 of cik, tag, dims, unit, start, end, accession) because a natural
unique key with a nullable start date does not dedupe under SQL NULL semantics.

**D-016 Metric lists are curated per industry group.** (User directive: the metric list for
each SIC is decided in YAML using domain knowledge, optionally LLM-assisted, then reviewed.
It is never generated at run time.) `metrics.yaml` defines metrics and named `metric_sets`.
Each group in `industry_groups.yaml` lists its sets plus extra codes, and
`company_overrides/` adds company-specific metrics. Helper metrics such as `ebit` are pulled
in automatically when a listed metric depends on them.

**D-017 Rules adopted from the team's SEC_metrics repo**
(github.com/wlvh/SEC_metrics; reviewed for domain rules only):
- Total debt: use a reported total first. Otherwise sum only within one tag family
  (`LongTermDebtAndCapitalLeaseObligations` + its `…Current`, or `LongTermDebt(Non)Current`),
  plus short-term borrowings / commercial paper. Families are never mixed (e.g. no
  `…Noncurrent` + `DebtCurrent`). If a family has no current sibling, the non-current amount
  alone is used, which understates debt.
- Interest coverage is N/M when EBIT ≤ 0. EBIT is operating income; when that is missing
  (Pfizer), it falls back to pre-tax income + interest expense.
- Ford: consolidated debt is dominated by Ford Credit (captive finance). D/E needs a reviewed
  industrial-scope override and is flagged in the coverage notes.
- Not adopted: Decimal/MetricSpec/ExecutionTrace machinery and the single-fiscal-year scope.

**D-018 Net income candidates:** `NetIncomeLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic`
→ `ProfitLoss`. Ford stopped tagging `NetIncomeLoss` in mid-2025. Its FY2025 value comes out
at −8,182M, which is the loss attributable to Ford (Ford has no preferred stock). The
SEC_metrics repo reports −8,162M because it uses `ProfitLoss`, which includes non-controlling
interests.

**D-019 `metric_value` conventions.**
- FY rows use `fiscal_quarter = 0`, which keeps the unique key NOT NULL.
- N/M values are stored as `value = NULL, value_status = 'NM'`.
- Growth metrics are stored as fractions (0.05 = 5%).
- A compute run upserts, then deletes only that company's rows that the current rules no longer
  produce. It never wipes the table.
- `derivation` is REPORTED / DERIVED / COMPUTED.
- `source_accn` holds the accession of the latest-filed input. A `source_document` link comes
  with Phase 3.
- The negative-equity risk signal that PLAN §6 asks for is created in Phase 3 (signals) from
  `value_status = 'NM'` on `debt_to_equity`.

**D-020 Golden values come from the filing.** Marriott's 10-Q for the quarter ended 2026-06-30
shows long-term debt of $16,455M plus $460M current portion, which equals the reported
`DebtAndCapitalLeaseObligations` of $16,915M. PLAN's "≈ $16.5B" was the long-term line. The
test asserts $16,915M total and $16,455M long-term; cash is $462M. FY2025 values for MAR, LUV
and Ford match the SEC_metrics repo, except Ford net income (see D-018).

**D-021 Fiscal-year labels follow the company's own convention.** The fiscal year is taken
from the `fy` that companies put on their own 10-K facts. For example, Macy's year ending
2026-01-31 is FY2025. Quarter ends of 52/53-week calendars (Pfizer 2026-06-28) map to the
nearest nominal quarter end within 10 days.

**D-022 Overnight updates when only an earnings 8-K exists → option B** (user decision,
2026-10-13). The requirement is that results released after the close appear on the dashboard
the next morning. 10-Q/10-K filings are already covered (D-013). Earnings arrive first as an
8-K Item 2.02 with an EX-99.1 press release that has no financial XBRL. For JPM the 10-Q
followed 2–3 weeks later.
- Headline numbers are extracted from EX-99.1 tables by the LLM.
- Each number must pass grounding: it has to appear in the press release text.
- Values are stored with `derivation = PRELIMINARY` and shown as preliminary.
- They are superseded automatically when the 10-Q/10-K XBRL values for the same period load.

Rejected options:
- A: wait for the 10-Q. JPM would show stale numbers for weeks.
- C: Alpha Vantage fundamentals. PLAN limits AV to cross-checks, and its latency is unverified.

Phase 3 downloads and stores EX-99.1 documents for 2.02 8-Ks. Extraction happens in Phase 5,
together with PLAN §6 Adjusted EBITDA option B.
