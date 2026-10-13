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

## Phase 3 (2026-10-13)

**D-023 Own section splitter instead of edgartools.** I evaluated edgartools 5.62 on the 9
latest 10-Ks:
- It failed on 2: LUV and SKYD, whose latest "10-K" is a Part III-only 10-K/A.
- It makes its own HTTP calls, bypassing the rate limiter, cache and raw landing
  (`clients/` rule).
- It pulls in 45 packages.

The replacement is `html_to_text` (lxml; block elements become lines; `display:inline` divs
and hidden iXBRL headers handled) plus item-heading detection:
- Repeated page headers ("Item 7 … (Continued)") are merged into one block.
- The longest block per item wins, which skips the table of contents.

Results on the latest original 10-Ks:
- Items 1A/3/7 were found for 27/27 (100%), and the auditor's report for 9/9.
- Sizes match edgartools where it worked (Ford 1A 94k / 7 159k chars vs 94k / 162k).
- Short Item 3 sections (JPM, LUMN, M, SKYD) are genuine cross-references to a litigation
  note in Item 8. Phase 5 legal analysis must follow them.

**D-024 Company section rules for non-standard layouts.** JPM's 10-K/10-Q body uses its own
headings and lists Items only in a page index at the back. `company_overrides/JPM.yaml`
`sections:` locates the MD&A by its own start and end heading lines (10-K MD&A 391k chars).
This is config, not code.

**D-025 Only original 10-K/10-Qs are split.** Amendments (10-K/A, 10-Q/A) are registered in
`source_document` but not split: they are often Part III only. All 10-K/10-Q/8-K filings
within `EVENT_LOOKBACK_DAYS` (730) are registered. Original 10-K/10-Qs within
`TEXT_LOOKBACK_DAYS` (400) are downloaded and split. 8-K bodies are downloaded only for the
items in `fetch_body_items`. SKYD documents start at `history_start` (D-012).

**D-026 EX-99.1 is found by document type, not file name.** File names vary too much
("er-6302026xerdoc.htm", "exhibit99tojuly282026for.htm"). The type column of the filing's
`-index.html` is used. Single-exhibit filers label it plain "EX-99", so the lookup tries
EX-99.1, then EX-99. Each company has 4 earnings releases in the last 400 days, stored as
`doc_role = 'EX-99.1'` for Phase 5 (D-022). One ENPH 2.02 8-K (2026-04-06, items 2.02 + 8.01)
has no exhibit; this is logged.

**D-027 Signals are rule-based, with rules in `config/signal_rules.yaml`.**
- 8-K items: mapped per PLAN §5.4. Item 3.01 is downgraded to Info "Listing Transfer" when the
  text describes a voluntary exchange transfer (LUMN and SKYD in 2026-09).
- 5.02 CEO/CFO rule: the role must belong to this company. A role followed by "of <other
  entity>" does not count; this excluded a JPM business-unit Co-CEO and a CFO title held at a
  previous employer. Sentences with "continue to serve" / "previously served" are excluded.
  The change verb must be within 100 characters of the role. A departure outranks an
  appointment within one filing. Sentence splitting ignores abbreviations (Mr., Jr., Inc.).
  On 2 years of 8-Ks for 9 companies this gives 7 CEO/CFO signals, all true positives after
  review; 4 false positives found during development became test cases.
- Auditor: name from `dei:AuditorName` (10-K XBRL instance) and tenure from the auditor's
  report ("served as … auditor since [at least] YYYY"). A name change between 10-Ks is a
  Warning.
- Keywords: going concern (auditor's report only), material weakness, controls not effective,
  restatement. Each rule is limited to specific sections and drops sentences with negation;
  none fired for the 9 companies.
- Metrics: negative equity (latest quarter), plus buybacks continuing while equity is negative
  (MAR, LUMN).
- `signal_key` = hash(company, rule, accession, section, extra). Reruns keep `detected_at`
  and delete only that company's signals that are no longer produced.

**D-028 Judgment signals: rules + LLM vote** (user decision, 2026-10-13: "切分完用 LLM 更
robust，或者都做然后 vote"; extractor Claude Sonnet 5.5, ties decided by Claude Opus 5.5).
- Scope (`config/llm.yaml` `topics`): judgment questions per section.
  - 8-K 5.02: CEO / CFO departure or appointment
  - 8-K 3.01: adverse listing notice vs voluntary transfer
  - Auditor's report: going concern
  - 10-K 9A / 10-Q Part I Item 4: material weakness, controls not effective

  Structural facts stay rule-only: 8-K item numbers, auditor name from XBRL, metric signals.
- Per document × topic:
  1. Rules vote.
  2. Sonnet 5.5 (effort low) reads the sections and answers with verbatim evidence.
  3. Only on disagreement, Opus 5.5 (effort high) rules. It sees both claims with their
     quotes, unlabelled and in a neutral order, and decides on the text.
  4. Majority wins. Three different answers → DISPUTED (kept, flagged for a human).
  5. A rule detection that both LLMs vote down is **kept as OVERRULED**, not deleted. The two
     models are correlated and could share a mistake, and a silently missing CEO/CFO change is
     worse for underwriting than an extra flagged item. (User raised this; agreed 2026-10-13.)
- LLM answers count only if their quote is found in the source text (whitespace, quote style
  and case ignored). Tie-break answers must also be in the topic's allowed set.
- Every call is cached in `llm_call` by a hash of (task, prompt version, model, effort, system,
  user, schema). Reruns on unchanged filings make no calls. Every vote is stored in
  `signal_vote`. Signals carry `method`, `review_status` and `votes_json`.
- If the LLM is unavailable (no key, bad credentials, bad request), the stage logs one error
  and continues with rules only (`review_status = RULE_ONLY`).
- Server-side refusal fallback (`fallbacks: "default"`) is enabled on both models.
- The key is a user-level key; `ANTHROPIC_WORKSPACE_ID` is sent as the `anthropic-workspace-id`
  header when set.
- **Succession** (added after the first live run): a departure and an appointment to the same
  role in one filing is answered `succession` (Warning), by the rules and by the LLM parsing.
  Before this, the code kept a single value per role, so a CFO handover looked like a
  disagreement between "departure" and "appointment".
- **Live run, 9 companies, 2 years of filings** (2026-10-13):
  - 89 documents and 185 topic votes; 179 were unanimous.
  - 6 went to a tie-break and Opus agreed with Sonnet in all 6:
    - 5 CFO successions where the rules saw only one side (MAR, LUV, F, PFE, SKYD)
    - 1 rule miss: Paramount Skydance's 2026-10-01 8-K appointing Ynon Kreiz Co-CEO
  - 0 OVERRULED, 0 DISPUTED, 0 ungrounded quotes.
  - Cost: about $0.41 in total. A rerun on unchanged inputs made 0 calls.
- Prompts: `signals_v1` (extractor), `tiebreak_v2` (adds the succession definition). A changed
  prompt gets a new version, which also invalidates its cache entries.

## Phase 4 (2026-10-13)

**D-029 News de-duplication in three tiers** (user: "news 应该有很多重复的，需要 dedup").
1. Same article URL after normalisation: tracking parameters (`utm_*`, `ncid`, …), scheme,
   `www.`/`m.`, AMP suffix and trailing slash removed.
2. Syndicated copies: within 72 h, word 3-gram Jaccard of the normalised title ≥ 0.8 (a trailing
   " - Reuters" style source tag is removed first), or title ≥ 0.5 and summary ≥ 0.8.
3. Same event, different articles: Claude Sonnet 5.5 groups headlines (id, date, source,
   headline only) into specific events. Ids it invents are ignored, members must be within the
   window, and results are cached in `llm_call`. Without an LLM, tiers 1–2 still run.

Each group becomes one `activity`, whose canonical article is the earliest new one (then the
most relevant). Every article is kept in `news_item` with its `dedup_method` (FIRST / NEAR_DUP
/ LLM_EVENT), so the UI can show "also reported by N". New articles attach to an existing
activity; two existing activities are never merged, which keeps IDs stable across runs.
Articles with AV ticker relevance < 0.3 are dropped. All thresholds are in `config/news.yaml`.
- **NEWS_SENTIMENT needs a premium Alpha Vantage plan.** The free key gets a "premium endpoint"
  message. The stage is built and tested against a synthetic feed in AV's documented format
  (`tests/test_news.py`, marked synthetic). The live acceptance check (each company has the
  last 30 days of news) is pending a premium key.

**D-030 Earnings call transcripts.**
- Latest 4 fiscal quarters per company, labelled with the company's own fiscal quarters from
  `metric_value`. AV uses the same convention: Macy's "2026Q2" is its quarter ended 2026-08-01,
  "Second Quarter 2026".
- Stored as `source_document` (source_type TRANSCRIPT, accession `AV-<ticker>-<quarter>`, URL
  without the API key) plus one `filing_section` "TRANSCRIPT" with one line per speaker turn,
  plus chunks.
- Raw responses are cached without a date (published transcripts don't change). An empty
  (unpublished) transcript is not cached and is asked for again after 7 days.
- Free plan: about 25 calls/day. The client keeps its own daily counter, and the stage stops
  cleanly when the quota is used and resumes on the next run. The first run stored MAR, LUV, F,
  PFE and M (20 transcripts); JPM, LUMN, SKYD and ENPH follow on the next run.
- Alpha Vantage echoes the API key in its rate-limit message. Messages are masked before they
  are logged or returned, and rate-limit messages are classified before "premium" ones (they
  mention premium plans too).

**D-031 No Alpha Vantage financial statements for now** (user decision). The XBRL-vs-AV
cross-check in PLAN Phase 4 is deferred. A first look showed MAR 2026 Q2 AV figures (revenue
7,071M, operating income 1,229M, interest 221M, D&A 222M) match our XBRL values.
