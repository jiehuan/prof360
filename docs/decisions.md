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

## Phase 5 (2026-10-13)

User: Phase 5 runs on my judgement until commit. Split into 5a (numbers from text) and 5b
(insights). Sonnet 5.5 handles extraction and per-item summaries, Opus 5.5 handles
company-level synthesis, and only bank capital/liquidity metrics get a two-model check.

**D-032 Numbers from text (5a).**
- Text metrics are curated per industry group like every other metric: `metrics.yaml`
  `kind: text` with `extraction` (definition, keywords, dual_check), and the `HOTEL_OPERATING`
  and `BANK_REGULATORY` sets.
- Sources: MD&A sections (10-K Item 7, 10-Q Part I Item 2) and the EX-99.1 earnings releases.
- Each metric first gets its 2 best keyword chunks; then the most-mentioned chunks fill a 60k-char
  budget. Without the per-metric step, frequent "CET1" chunks crowded out JPM's NIM wording.
- The model copies numbers as written, with unit, scale, period start and end. Code converts
  them. A value is kept only if its quote is in the source, the number as written is in the
  quote, the unit fits the metric, and the period length fits (quarter 75–105 days, year
  350–380). The period check is what rejected SKYD's Aug 8 – Sep 30 merger stub shown in a
  "quarter" column. Year-to-date values are skipped.
- Bank metrics (`dual_check`) and the loan book are kept only where Sonnet and Opus agree
  within 0.5%. When several documents report the same metric and period, the latest-filed one
  wins and disagreements are counted.
- Earnings releases (D-022): when a period already has XBRL, the release value is only
  cross-checked. Live run: 200 checks across 9 companies, 0 mismatches, which also confirms
  the scale handling. Otherwise the value is written as PRELIMINARY. Today the only case is SKYD
  FY2025 Q4: its stub-year 10-K can't yield Q4 from XBRL, so that release value stays.
- The compute stage now deletes only rows it owns (REPORTED/DERIVED/COMPUTED). The text stage
  owns LLM_EXTRACTED/PRELIMINARY, so a later 10-Q upsert replaces a PRELIMINARY value.
- Results:
  - MAR: RevPAR, occupancy and ADR for 5 periods. FY2025 is RevPAR $128.80 and occupancy 69.3%,
    the same as the SEC_metrics repo.
  - JPM: CET1, Tier 1 and LCR for about 8 quarters, NIM for 8 periods, NPL ratio for FY, and the
    loan book by segment (Consumer ex-card $367bn, Card $250bn, Wholesale $847bn at 2026-06-30).
- Prompt `text_metrics_v2` adds the period start.

**D-033 Insights (5b).**
- Package (`enrich/package.py`): every input item has an id.
  - M: metric rows (latest 5 quarters + 3 fiscal years, formatted as shown to users)
  - S: signals, with OVERRULED/DISPUTED marked
  - E: filing excerpts (risk factors up to 120k chars, legal, 10-Q MD&A start)
  - T: the latest 3 earnings call transcripts
  - N: news activities from the last 30 days
  - B, R, K: insights from earlier steps
- Tasks, in order:
  1. activity summaries (Sonnet 5.5, batches of 20)
  2. behavioral insights from transcripts (Opus 5.5)
  3. strategic & risk insights for 3 categories, with explicit "no signal" (Opus)
  4. key insights, 6–10 (Opus)
  5. follow-up questions, 10–15 (Opus)

  Prompts are `common_v1` (shared rules: no underwriting decisions, cite everything, numbers
  only from the input) plus one versioned prompt per task.
- Grounding (`enrich/insights.py`, `grounding.py`):
  - An item needs at least one citation whose quote is found in the cited item. Metric rows
    accept short quotes such as "$16.91B"; other items need at least 8 characters.
  - Every financial number in the text must match a package metric value (allowing unit
    conversion and rounding to the precision written), or appear as written in a cited item.
    The digits are matched with boundaries.
  - Years, quarter numbers and small counts are ignored.
  - Failing items are dropped and logged.
  - A citation of an earlier insight (R/B) is stored as that insight's own evidence, so
    lineage always ends at a document, metric, signal or news item.
- Versioning: a set is stored only when its input hash changes. The hash includes the grounding
  version, prompts, model and the rendered input. A new set becomes `is_current` and older sets
  are kept. An unchanged input writes nothing, and its LLM output comes from the cache.
- Bug found in the first live run: `strict_json_schema` removed the schema annotation key
  "title" and also dropped fields named `title`. That made the behavioral output fail
  validation. It is fixed and has a regression test. Earlier schemas had no `title` field.
- Acceptance tooling:
  - `uw-pipeline audit-insights` re-checks a random sample's evidence against the sources,
    checks that no insight lacks evidence, and checks that no FI data exists for non-FI
    companies.
  - `scripts/export_golden.py` writes MAR/JPM/ENPH outputs to `tests/golden/` as the
    regression baseline for prompt changes.
- **Live run, 9 companies (2026-10-13).** Kept / generated per company:
  - Behavioral (5 companies with ≥2 transcripts): 6/6
  - Strategic & risk: 9–12 of 12
  - Key insights: 9–10 of 10
  - Follow-up questions: 14–15 per company
- **Grounding dropped 8 items**, e.g. JPM CEO pay ($43M), which is not in the input (no proxy
  data) and so came from model knowledge, and ENPH "$1.08B safe harbor deals". Two fixes during
  the run:
  - Grounding compared "1819m" instead of the digits, so "$1,819M" vs "$1,819 million" failed.
  - Bare integers under 100 (days, counts) were treated as financial figures; they are now
    ignored.

  Each fix bumped `GROUNDING_VERSION` and re-grounded the cached outputs.
- **Acceptance:**
  - `audit-insights`: 20 sampled insights, 107/107 evidence snippets found in their sources;
    0 insights without evidence; 0 FI rows for non-FI companies.
  - A rerun with unchanged inputs made 0 LLM calls and wrote 0 rows.
  - Golden outputs for MAR/JPM/ENPH were exported to `tests/golden/`.
- **Cost:** all LLM calls so far total about $17.7, including development reruns (text-metric
  prompt v1→v2, the NIM definition change, the schema bug).
  - Biggest items: strategic & risk ($4.4 for 9 companies, ~70k input tokens each), text
    metrics ($6.7 including reruns) and key insights ($2.5).
  - A full first run for one company is about $1.2–1.5 with Opus 5.5 for synthesis. Nightly
    cost depends only on which companies have new inputs.
- **Open:**
  - Activity summaries are built and tested but have no live data until the premium Alpha
    Vantage key arrives.
  - Behavioral insights for JPM, LUMN, SKYD and ENPH wait for their transcripts (quota).
  - Secure GPT is still to be added as an `LLMClient` implementation.

## Phase 6 (2026-10-13)

User: image registry, namespace and Harness details are placeholders for now; dev runs on SQLite;
schedule as proposed.

**D-034 Orchestration and deployment.**
- **Run/task state machine** (`jobs/orchestrator.py`):
  - One `pipeline_run` per job run, and one `pipeline_task` per (company, stage), executed
    stage by stage.
  - Stage prerequisites are declared in `DEPENDS`, e.g. insights ← signals, textmetrics. A
    company whose prerequisite failed has its later stages SKIPPED; other companies continue.
    A transcripts failure (quota) does not block insights.
  - A failed task is retried immediately up to `max_attempts` (2), unless the failure is
    permanent (quota, premium endpoint, missing key).
  - The heartbeat is updated on every task transition.
  - Starting a job whose last run is RUNNING with a fresh heartbeat (< 30 min) exits with
    code 2 (the run lock). With a stale heartbeat, i.e. the process died, the start resumes
    that run: interrupted and skipped tasks go back to PENDING, finished tasks are kept, and
    `starts` is incremented.
  - Tested with a real `kill -9` of a subprocess mid-run and a restart: every task completes
    once, and finished tasks do not run again.
- **Jobs** (`config/jobs.yaml`) on OpenShift CronJobs with `timeZone: America/New_York`:
  - `nightly` at 23:30 ET. SEC accepts filings until 22:00 ET, so the evening's 10-Q or
    earnings release is on the dashboard the next morning (with D-013 and D-022).
  - `morning` catch-up at 05:30 ET.
  - `news` every 6 h, suspended until a premium AV key exists.

  `concurrencyPolicy: Forbid`. `backoffLimit: 2`: a retried pod resumes the same run.
- **Logs:** `LOG_FORMAT=json` gives one JSON object per line with `run_id`, `job`, `ticker`
  and `stage` (contextvars). Local logs stay as text.
- **Run statistics** (`stats_json`): task counts per status, SEC requests, Alpha Vantage calls,
  and LLM calls/tokens per model with a cost estimate (prices in `llm.yaml`). Shown by
  `uw-pipeline runs`.
- **Image:** a multi-stage Dockerfile. The base image is a build arg (Artifactory
  placeholder). It installs the Microsoft ODBC Driver 18 for Azure SQL (repo URL is a build arg
  for an Artifactory mirror), runs as a non-root, group-0-writable user for OpenShift, and
  bakes `config/` into the image so config is versioned with code. The default command is
  `run --job nightly`.
  - Not built locally, because no Docker daemon was available. The wheel was built and
    installed in a clean venv, `uw-pipeline` ran, and all 13 prompt files are packaged.
- **Manifests** (`deploy/openshift/`): PVC (raw data and dev SQLite), secret example (keys
  only), and the 3 CronJobs. `deploy/harness/pipeline.yaml` is a skeleton: lint, types and
  tests; build and push; apply the manifests. Placeholders are marked `PLACEHOLDER_*`.
- **Not verifiable here:** "dev CronJob succeeds 3 days in a row" needs the team's cluster.
  SQLite allows one writer, so jobs must not overlap until Azure SQL; news stays suspended.
- **SEC refresh for scheduled jobs** (bug found in the first live nightly run):
  - The raw cache is keyed by UTC date, and nightly (23:30 ET = 03:30 UTC) and morning (09:30
    UTC) share a UTC date. Morning would therefore have reused nightly's metadata and missed
    filings made in between.
  - Fix: jobs with `refresh_sec: true` (nightly, morning) always re-fetch submissions,
    companyfacts and tickers. Each fetch lands under a timestamped key, so every version stays
    replayable. Manual runs keep the same-day cache, and filing documents stay cached
    (immutable).
- **Live verification, 2026-10-13:** run 1 (`nightly`) and run 2 (`morning`). Both finished
  68/72 tasks; the 4 transcripts tasks failed on the AV daily quota, which is permanent for
  the day, so they were not retried. Run 2 sent 59 SEC requests and picked up the filings
  made that day:
  - JPM's Q3 2026 earnings release (8-K 2.02). Text metrics wrote PRELIMINARY Q3 revenue
    $50.17B (reported basis) and net income $15.31B, plus CET1 13.9% (dual-checked).
  - SKYD's 8-K (1.01/2.03, new notes and credit facilities).

  Only those two companies' insights were regenerated (8 LLM calls, about $1.31), and every
  other company was "unchanged". This is the overnight-update path from D-022, end to end.

## Phase 7 (2026-10-13)

User: data-quality rules and an operations guide now; BFF integration waits for the BFF repo
and the Phase 0 contract.

**D-035 Data quality.**
- The `dq` stage runs at the end of the nightly and morning jobs. Rules are in
  `config/dq_rules.yaml`, findings go to `dq_finding` per run, and `uw-pipeline dq-report`
  writes the report. Findings are data, so the task succeeds even when it finds issues.
- Rules:
  - missing core metric (uses the coverage notes)
  - quarter jump: > 500% both vs the prior quarter and vs a year earlier, on core metrics with a
    meaningful base, which catches scale/sign errors but not seasonality
  - negative value of a positive-only metric
  - four quarters ≠ fiscal year (> 0.5%)
  - earnings release ≠ XBRL (new `release_check` table)
  - LLM grounding drop rate > 20% in 30 days (new `llm_task_stat` table)
  - no 10-K/10-Q for 120 days
  - review queue (DISPUTED / OVERRULED signals)
- The XBRL-vs-Alpha-Vantage rule from PLAN Phase 7 is not implemented (no AV financials, D-031).
- Tuning from the first live run:
  - The coverage window is anchored on XBRL periods, so JPM's PRELIMINARY Q3 2026 no longer
    shifts it.
  - Text metrics target the last 4 quarters.
  - The quarter-jump rule went from 30 findings, mostly seasonality and one-off charges, to 3,
    all real events: Ford's Q4 2025 operating loss and net loss, and LUMN's Q2 2025 operating
    loss.
- **Known limitation found by DQ.** LUV's 2026 10-Qs restated 2025 Q1/Q2 capex (comparatives
  501→527M, 635→660M), while FY2025 and Q3 are still on the old basis. "Latest filing wins per
  period" therefore mixes bases, and the derived Q4 makes the quarters disagree with the year
  (2,724M vs 2,673M). It is flagged as an Error, not auto-corrected. A proper fix would take
  every period of a fiscal year from the same filing when a restatement is detected.
- `docs/schema-contract.md` maps front-end modules to tables and columns, with "no data"
  behaviour, as the starting point for BFF alignment. `docs/OPERATIONS.md` is the operations
  guide (Chinese).

**D-036 Detail modals (Phase 8).** The front end's five detail modals (metric, Loan Portfolio
Composition, Strategic & Risk / Behavioral insight, Key Insight, Follow-Up Question) read one
set of links between metrics, insights and questions, and one set of source titles and links.
- **Rules first.** Methodology, Data Sources, Supporting Evidence, Sources and the loan book's
  Raw Data Points are built by rules (`enrich/sources.py`, `enrich/metric_detail.py`,
  `config/source_labels.yaml`): traceable and free. The LLM writes only what needs judgment:
  each metric's Underwriting Relevance and its related insights/questions (one Claude Opus 5.5
  call per company, `metricdetail` stage), and the new question fields (`followups_v2`:
  summary, following risk, UW relevance, related metric rows). Numbers are checked as before.
- **Source titles** follow "<company> <form> filed <date> · <section>" and link to the SEC
  document; values from XBRL link to their filing (EDGAR filing index when the filing is older
  than the document window). Transcripts have no public link (the Alpha Vantage URL needs a
  key) and get a title only; their date is unknown, so an insight resting only on transcripts
  has no Updated At.
- **Links.** `insight_link` (key insight → the R/B insights it cites), `insight_metric` (now
  also the metric rows an insight cites, plus `link_source = METRIC_DETAIL` rows from the
  metric stage), `followup_question_metric` (metric ids M… chosen by the model, mapped to codes
  by the pipeline, so the model never guesses a code), `followup_question_source` (the evidence
  of the question's insights plus the filings behind its metrics). Database ids of the cited
  objects are part of the key-insight, question and metric-relevance hashes, so links follow
  re-stored objects even when the text is identical (the LLM output then comes from the cache).
- **FI.** Fee income = total noninterest income (`us-gaap:NoninterestIncome`), with YoY growth
  for Performance Trends. Banks keep the MD&A of the last 5 years of 10-Ks
  (`mdna_history_years`, only that section, so no extra signal votes or insight excerpts) for
  5 years of NIM / CET1 / NPL; values still need both models to agree. The loan book is now by
  loan class with its parent segment, for the latest period and the prior year-end, with a
  nonaccrual ratio; only classes with a reported balance are kept (JPM's MD&A splits wholesale
  by class only as allowance percentages, so wholesale stays one row; balances are never
  computed from percentages).
- **DQ** (`detail_coverage`, `source_coverage`, `link_integrity`) and `audit-insights` check
  that every displayed metric with data has methodology and sources (≥ 90% with relevance),
  every insight and question has a source, links reach current objects, and loan rows are found
  in the cited filing.

**D-037 Application database, news quality and risk scores (Phase 9).** User decisions: the
pipeline replaces the left half of the team's data model diagram; the score tables are wanted;
the diagram may change, as little as possible; scores 0.00-1.00 (tone -1.00..1.00); RiskTypes
by config; confidence by rules; only material news feeds the insights.
- **Publish stage.** `publish` writes the application tables (`schema_app.py`) from the pipeline
  tables, one transaction per company, in the app database (`APP_DATABASE_URL`; dev
  `data/app.db`). App ids are identity columns; `pipeline_publish_map` (in the app database, so
  a row and its mapping commit together) maps every pipeline object to its app id and stores a
  content hash, so reruns update the same row and unchanged rows are not written. Key insights,
  strategic risks and questions are versioned: a new version is a new row, older rows get
  `isActive = 0`, so user flags never end up on a question whose text changed.
- **Diagram changes:** new `ItemLink` (one table for every Related tab) and
  `CompanyMetricDetail` (metric modal; the loan modal as pseudo-metric `loan_portfolio`); added
  columns on LoanPortfolio/LoanTypes, FollowUpQuestions (Priority, isActive), KeyInsights
  (isActive), NewsRiskScore (ActivityId), TranscriptRiskFlag (Category),
  TranscriptToneAnalysis (CompanyId, Quarter); sources columns hold `[{title, url, date}]`.
- **RiskTypes** come from the model's free-text subcategory through ordered regex rules in
  `config/app_publish.yaml` (no prompt change, no regeneration). On the live data every
  subcategory maps to a named type.
- **Confidence** by rules (`config/app_publish.yaml`): metric values by derivation, insights by
  number and kind of sources, activities by number of articles.
- **News quality.** With the premium key, AV relevance proved useless for large companies: 1,893
  JPM articles in 30 days, mostly other banks or JPMorgan as analyst / fund holder. Two filters:
  (1) rules — the article must name the company (`news_aliases`, case-sensitive) and the
  headline must not match analyst-action or holdings patterns; (2) a Claude Sonnet 5.5 verdict
  per stored article (SUBJECT / PASSING / ANALYST / HOLDING / MARKET), judged once, only SUBJECT
  kept. Result: 379 activities for 9 companies (JPM 1,714 → 117). Stored articles are re-checked
  against the current rules on every run (`pruned`).
- **Risk scores (Sonnet 5.5, anchored rubrics).** News: five dimensions plus overall, per
  activity, rationale numbers checked (a failing rationale is withheld, scores kept, so the item
  is not re-sent every run). Transcripts: per chunk (6 chunks per call), length-weighted mean;
  risk flags must be verbatim quotes. Bounds are in the descriptions and clamped in code
  (structured outputs do not enforce numeric bounds).
- **Materiality gate (option C).** News enters the strategic/key/follow-up input only with
  overall ≥ 0.5 or any dimension ≥ 0.6 (`config/insights.yaml`), so routine news does not
  regenerate the Opus insights (measured ~$0.93 per company per regeneration). 0.6/0.7 admitted
  4 items and missed an SEC censure of JPM and class actions against Ford; 0.5/0.6 admits 13.
- **Schedules.** `uw-news` (news, scores, publish) runs at 03/09/15/21 ET, between nightly and
  morning, because SQLite has one writer.

**D-038 Cheaper models (user decision, 2026-10-13).** Claude Haiku 5.5 replaces Claude Sonnet 5.5
(extractor: signal votes, text-metric first pass, news grouping and relevance, activity
summaries, risk scores) and Claude Sonnet 5.5 replaces Claude Opus 5.5 (tie-breaker and second
model of the dual check, and the synthesis tasks: behavioral, strategic & risk, key insights,
follow-ups, metric relevance). Config only (`llm.yaml`, `insights.yaml`).
- Haiku 5.5 rejects the `fallbacks` parameter (400), so the client sends server-side fallback
  only to models that support it.
- The dual check still pairs two different models (Haiku + Sonnet).
- The model is part of every cache key, so the switch re-ran all LLM tasks once. Stored news
  relevance verdicts and risk scores are keyed by their text and prompt version, not the model,
  and were kept.
- Text shown to users no longer names models ("two different models").
- **Results of the re-run** (compared with the Sonnet/Opus outputs):
  - Signal votes: all 102 signal outcomes identical (89 Haiku calls, 8 Sonnet tie-breaks).
  - Text metrics: no value disagrees with the earlier extraction. At `low` effort Haiku lost
    15 of ~94 values: it skipped comparative columns and quoted loosely, joining a table header
    and a row with "..." and leaving out footnote markers ("capital3"). Grounding now accepts
    elided quotes (every piece verbatim, in order, ≥ 8 characters) and ignores footnote markers
    glued to words (`GROUNDING_VERSION` 5), and text extraction runs Haiku at `medium`
    (`extractor_effort`). Remaining gap: three JPM 2025 Q1 comparatives.
  - Synthesis (Sonnet): grounding dropped 17 of 89 key insights (about 19%, Opus about 5%),
    almost all for figures the model computed itself (sums, ranges), which the prompt forbids.
    Every company still has 6-10 key insights. If the drop rate stays near the 20% DQ
    threshold, a stricter prompt version is the next step.
  - Cost of the full re-run: about $6.

**D-039 SecureGPT instead of the Claude API (user decision, 2026-10-14).** LLM calls go through
the company's SecureGPT gateway (Azure OpenAI behind the AXA XL API gateway). gpt-5-nano
replaces Claude Haiku 5.5 (extractor) and gpt-5-mini replaces Claude Sonnet 5.5 (tie-breaker,
second model of the dual check, synthesis).
- `clients/securegpt.py` implements the same `complete_json` interface, so no stage changed.
  Protocol as in the team's working client (professional-databricks `utils/secure_gpt.py`):
  OAuth token from `token_url` (Client_Id / Client_Secret / scope / subscription key headers),
  then `{base_url}/{deployment}/chat/completions?api-version=2024-10-21`. Differences on
  purpose: TLS is verified (theirs uses `verify=False`), the token is refreshed before expiry
  and after a 401, 408/429/5xx are retried with Retry-After, and failures raise instead of
  printing.
- Output: OpenAI structured outputs (`json_schema`, strict). All output schemas are checked
  against strict-mode rules in a test.
- Errors: refusals, truncation and content-filter blocks are per-item (`LLMError`, recorded,
  skipped); rejected credentials, unknown deployments and other 400/403/404 stop LLM use for
  the run (`LLMSetupError`).
- `reasoning_effort` is off for api-version 2024-10-21 until the smoke test
  (`scripts/securegpt_smoke.py`) shows the gateway accepts it. `max_tokens` limits were raised
  because gpt-5 counts reasoning tokens in them.
- Credentials only in environment variables (`SECUREGPT_*`); non-secret settings in
  `config/securegpt.yaml`. The production gateway URLs are assumed from the dv/pp pattern and
  must be confirmed.
- Cost statistics in EUR (SecureGPT price list: nano 0.052 / 0.378, mini 0.240 / 1.889 per
  million tokens). The Claude client stays available (`provider: anthropic`).
- The development machine cannot reach the gateway, so the switch is verified from the work
  environment: smoke test first, then a full re-run compared with the current outputs, as for
  D-038. gpt-5-nano is expected to need a higher effort (or gpt-5-mini) for text extraction.
- Security note: the team repository's `config/utils.yaml` holds what look like plaintext APIM
  subscription keys; they should be rotated and moved to a secret store. None were copied here.
- **Both providers stay usable** (user decision, 2026-10-14): `config/llm.yaml` keeps one
  complete profile per provider (extractor, tie-breaker, synthesis, prices); `LLM_PROVIDER`
  selects it, default `securegpt`. Development and tests off the corporate network use
  `anthropic` (set in the local `.env`); each profile keeps its own cached results, so
  switching back re-uses them without new calls. The synthesis model moved from insights.yaml
  into the profiles.

**D-040 Azure SQL and ADLS (Phase 12, 2026-10-14).** Same approach as the team's
professional-databricks code (pyodbc with a service principal; azure-identity +
DataLakeServiceClient), configured for the pipeline.
- **Settings.** `config/azure.yaml` holds the non-secret server, database, schemas, storage
  account, container and root per environment (dv / pp / pr, `AZURE_ENV`). The values are
  placeholders copied from the loss-run project, marked TO EDIT (user decision: fill in later).
  Credentials only from the environment (`AZURE_SQL_CLIENT_ID/SECRET`, `ADLS_CLIENT_ID/SECRET`).
  `DATABASE_BACKEND=azure_sql` and `STORAGE_BACKEND=adls` switch the deployment over; off the
  network the defaults stay SQLite + local files, like `LLM_PROVIDER` (D-039).
- **Server certificate verified** (`Encrypt=yes`, `TrustServerCertificate=no`), unlike the
  team code's `TrustServerCertificate=yes`.
- **Two schemas.** Pipeline tables and application tables are in separate schemas
  (`pipeline_schema`, `app_schema`); the table definitions have no schema and each engine maps
  them (`schema_translate_map`). Required, not only tidy: SQL Server names are case-insensitive,
  so the app's `Company` and the pipeline's `company` would be the same table.
- **Verified on a real SQL Server** (2022, local Docker, `scripts/test_mssql.sh`): the whole
  unit-test suite (220 tests) runs unchanged against it, plus SQL Server-specific tests (MERGE,
  large deletes, Unicode). Found and fixed:
  - the production image could not install ODBC Driver 18 on Debian 13 (repository signing
    key) and the driver missed `libgssapi-krb5-2`;
  - `IS 1` for boolean filters (now `= 1`), multi-column `IN` (now chunked OR/AND, within the
    2,100-parameter limit), a boolean `ORDER BY`;
  - nested queries while a result was still open ("connection is busy", no MARS);
  - pyodbc `fast_executemany` truncating NVARCHAR(max) values (long-text rows go without it);
  - all strings are NVARCHAR now (curly quotes, non-ASCII names).
- **DDL** for both schemas is generated (`scripts/export_app_ddl.py` → `deploy/sql/`), runs
  cleanly on SQL Server and is idempotent; the engineer applies it, the pipeline does not create
  tables in shared environments.
- **ADLS** (`AdlsStorage`): same keys as local storage under a root prefix, so `raw_path`
  values stay valid; writes go to a temporary name and are renamed into place.
- `scripts/azure_smoke.py` checks connections, schemas, tables, permissions and ADLS read/write
  on the corporate network before the first run.

**D-041 Code review fixes (2026-10-14).** A review of the whole repository (after Phase 12)
found 38 issues. 34 are fixed, most with a regression test; 4 are left as they are (end of this
entry). Grouped by what they affected:
- **Runs** (`jobs/orchestrator.py`, `config/jobs.yaml`).
  - **One run at a time, across jobs.** A live run of any job (fresh heartbeat) blocks a new
    start (exit code 2). Before, only the same job was blocked, so the 4x/day news job could run
    alongside nightly and both wrote news, scores and the app tables.
  - **Bounded resume.** A dead run is resumed only by the same job with the same stages and
    companies, and only within `resume_within_hours` (12) of its start. Older or different dead
    runs are closed (FAILED, `"abandoned": true` in stats) and the new run starts fresh, instead
    of continuing a days-old run with stale inputs.
  - **Heartbeat during a task.** A background thread refreshes `heartbeat_at` every
    `heartbeat_minutes` (5). A single long task (insights for all companies) took longer than
    `stale_after_minutes`, so a second start could take the live run for dead and run it twice.
- **LLM calls** (`enrich/llm.cached_call`).
  - Outputs are schema-validated before they are cached. An invalid output was cached and
    re-served on every run; it is now recorded as an error and asked again next time.
  - On SQL Server the cache row is written in its own transaction, so a stage that fails and
    rolls back keeps the calls it already paid for.
  - A request too large for the model (one very long document) fails that item only; it used
    to switch the LLM off for the rest of the run. Same for SecureGPT's context-length 400.
- **Grounding** (`GROUNDING_VERSION` 6; stored insights are re-checked once, without LLM cost).
  - Footnote markers are stripped only when glued to a word or in parentheses: a period label
    such as "2025" could be taken for a footnote.
  - Numbers match only whole numbers: "5" no longer matches inside "2.5" or "15", in insight
    checks and in text-metric quotes.
- **Data.**
  - Negative-equity signal: used the latest quarter even when that quarter only had preliminary
    results without equity, so negative equity went unreported. Now the latest quarter that has
    an equity value.
  - Text metrics: when one model's answer is missing for a source, the earlier values are kept
    instead of deleted; the fiscal calendar uses the company's fiscal year end (was always
    December); a period XBRL reports as not meaningful (N/M) is not filled from text.
  - Transcript tone scored on only some chunks is not cached as complete, so the rest are
    scored next run.
  - A re-split document drops the sections it no longer finds (and their chunks). After a
    `sections:` override fixed a false match, the old section kept feeding signals.
- **Database writes.**
  - Upserts drop repeated keys within one batch (a SQL Server MERGE fails on them).
  - Deletes follow foreign keys: news risk scores before their activities (pipeline) and
    before the News rows they point to (app).
  - Publish: `NewsRiskScore.ActivityId` holds the app's `Activities.ActivityId` (it held the
    pipeline's id). `TranscriptRiskFlag` rows are written only when they change (they were
    deleted and re-inserted on every publish), and flags of transcripts no longer scored go.
  - The app writer checks existence against the company's own rows, not whole tables, and does
    not re-examine retired versions (`isActive = 0`) on every publish.
  - SQL Server staging inserts use `fast_executemany` with explicit parameter types. Long text
    no longer needs the slow path, and timestamps keep their microseconds (fast mode dropped
    them).
  - Result sets are read in full before the next query (audit, golden export), and Azure SQL
    connections enable MARS, against "connection is busy".
- **Security and operations.**
  - Alpha Vantage HTTP errors no longer carry the request URL, which contains the API key and
    could reach logs and `pipeline_task.error`.
  - A misspelt `DATABASE_BACKEND` / `STORAGE_BACKEND` fails at startup instead of silently
    using SQLite / local files.
  - ADLS: a failed clean-up of a temporary file no longer hides the original error;
    `Storage.delete` replaces the smoke test's reach into the client.
  - `azure_smoke.py`: ALTER on the pipeline schema is a warning, like CREATE TABLE (runs use
    `#temp` tables only). `init-db` on Azure SQL points to `deploy/sql/` instead of a traceback.
  - `test_mssql.sh` waits for a working login; the log line it waited for survives a restart.
  - `export_app_ddl.py` compiles the original table definitions, so a rerun gives the same files
    (copies listed constraints in random order). Automatic index names in
    `pipeline_schema_full.sql` are now `ix_<table>_<column>`, as `create_tables` makes them
    (were `ix_uw_pipeline_<table>_<column>`); no shared environment had applied the scripts.
- **Left as they are.**
  - Q4 = FY − 9M can combine a restated full year with nine months as first reported (D-035).
    The `quarters_vs_year` DQ rule shows such cases; they usually resolve with the next 10-K.
  - An insight number may come from the cited item's text, not only the quoted sentence: by
    design (D-033); the docstring now says so.
  - SEC data is fetched again for every company on every nightly run. Fine for the 9 current
    companies; part of the scale-out plan (Phase 10).
  - One finding was judged not reachable with the inputs the pipeline produces.

## Phase 13 (2026-10-14)

User: after comparing with the team's SEC_metrics repo, adopt three of its ideas: structured data
before text, identities between metrics, pay versus performance. Decisions: text values that
break an identity are dropped (XBRL values only reported); pay data feeds insights, follow-ups
and a signal but is not shown as metric rows; add the pay/performance signal.

**D-042 Structured first, identities, pay versus performance.**
- **Capital ratios from XBRL.** CET1 and Tier 1 (plus total capital, not displayed) come from the
  10-K/10-Q XBRL instances: the firm (parent company, consolidated) under the Standardized
  approach, unit `pure`. Banks name the methodology members themselves, so the candidates are in
  `company_overrides/JPM.yaml`; a bank without them keeps the text extraction.
  - Banks also tag the regulatory floors and buffers (e.g. CET1 minimum 11.4%) with the same
    unit, period and dimensions as the actual ratios. A `pure` concept refuses any candidate
    whose name contains Minimum / Required / WellCapitalized / Buffer / Surcharge, and a test
    on the real 10-K excerpt checks the actual ratios win (0.146 / 0.155 / 0.174, FY2025).
  - A formula metric with an `extraction` is still read from the text, but stored only for
    periods XBRL does not cover (an earnings release before the 10-Q: PRELIMINARY);
    elsewhere it is cross-checked against XBRL in `release_check` (DQ `release_vs_xbrl`).
  - LCR, NIM and NPL have no usable XBRL tag and stay text-extracted.
- **Identities** (`config/dq_rules.yaml`, `metric_identities`): RevPAR = ADR x occupancy (5%);
  CET1 <= Tier 1 <= total capital; the loan book by class may not add up to more than total
  loans (`total_loans`, XBRL, 1%). The textmetrics stage drops text values that break one and
  does not store such a loan book; the dq stage reports stored values that break one (Error)
  and the drops (Warning). The JPM loan classes add up exactly to total retained loans
  (1,463,808M at 2026-06-30), and all MAR periods hold the RevPAR identity within 0.06%.
- **Pay versus performance** (proxy statement DEF 14A, `ecd` taxonomy): CEO total and
  actually-paid compensation, other executives' average, company and peer TSR (value of $100),
  per fiscal year, from the latest two proxies (each covers five years).
  - PEO amounts are tagged without dimensions (JPM, Ford) or per person on `ecd:IndividualAxis`
    (Marriott). One person in the year counts; two or more (a mid-year CEO change) give no
    value, since adding them up would be wrong. (SEC_metrics marked Marriott as having no
    `ecd` data; its PEO amounts are tagged per person.)
  - Not displayed (`display: false`), but `insight_input: true` puts them in the insight
    package, so insights and follow-ups can cite CEO pay (before, such figures failed
    grounding: JPM CEO pay in Phase 5).
  - Signal `pay_vs_performance` (Warning, `config/signal_rules.yaml`): CEO total compensation
    up at least 25% year on year while TSR fell.
- **Found in the live run (2026-10-14) and fixed:**
  - Southwest lists both CEOs every year, the one not in office with 0: a person with 0 is not
    counted as a second PEO. Its 2022 (a real mid-year change) still has no value.
  - Insights quoting "value of $100 invested" failed number grounding ($100 is in the metric's
    name, not its value): metric names now count as input (`GROUNDING_VERSION` 7).
  - `link_integrity` warned about links to the pay metrics; links to `insight_input` metrics are
    expected (they are not published).
  - Skydance has no pay data: no DEF 14A in three years, and its 10-K/A Part III has no `ecd`
    tags (newly public successor).
- No table changes. Insights regenerate once for companies whose input changed (live run:
  $5.86 + $0.40 + $0.64 with Claude).
- Not adopted (for now): axis-anchored matching of other banks' members (wait for a second
  bank), the status vocabulary (needs a BFF contract change), cross-CIK 8-K retrieval.

## Phase 14 (2026-10-14)

User, after a second look at the team's SEC_metrics repo: do all of it. Decisions: show ROA,
ROE and the loan-loss reserve ratio; AUM, VaR, international revenue, capacity utilization, RPO
and board composition are insight input only; review by command line with a decision table;
holdout answers taken from the filings themselves; daily LLM budget $25; board signals for a
CEO-chair without a lead independent director and for independence below two-thirds. No live
LLM runs until the end, then one comparison.

**D-043 Coverage from XBRL, traits, budget.**
- Bank metrics from XBRL formulas: ROA and ROE on average assets / equity (`x_py` in a formula
  is x a year earlier), allowance for loan losses / loans, noninterest / net interest income.
  JPM FY2025: ROA 1.35%, ROE 16.1% (total equity, preferred included), reserve ratio 1.83%.
- RPO (`us-gaap:RevenueRemainingPerformanceObligation`) for groups with the contract_revenue
  trait (telecom, semiconductors; Lumen and Enphase tag it).
- Traits (`industry_groups.yaml`): a group names its business-model traits (bank, lodging,
  contract_revenue, manufacturing, captive_finance) and gets their metric sets; governance
  applies to every group. Scales to new groups better than listing sets.
- Daily LLM budget (`LLM_DAILY_BUDGET`, default 25 in the profile's currency): over it, each
  new call fails as an item and waits for the next day; cached results are still served.
  Single-flight calls wait for Phase 10: the global run lock means no concurrent calls today.

**D-044 Tables read by cell; source text is data.**
- `normalize/table_grid.py` turns filing tables into grids (spans expanded, hidden content
  dropped). For metrics with `extraction.table`, the model answers with table, row and column;
  `enrich/table_reader.check_cell` accepts the answer only if the cell holds the number, the
  metric is named in the column header or row label, the column is not a change column, the
  lowest dated header line shows exactly the claimed year, the month and period length match,
  the row is in scope and not a subset (`exclude_rows`), and the table does not call itself
  illustrative or pro forma. A rejected answer is withheld, never repaired from other cells.
- Moved to this path: RevPAR, occupancy, ADR (prompt `table_reader_v1`) and the loan book
  (`loan_table_v1`; the balance and nonaccrual columns are checked separately). New: average
  interest-earning assets, AUM, average total VaR, international revenue (firm-wide note),
  capacity utilization (no data for Ford, whose filings have no such table).
- Holdout: Hilton and Hyatt filings (layouts the rules were not written for) plus Marriott and
  JPM: 14 right cells accepted, 18 plausible wrong ones withheld (regions, company-operated
  subsets, change columns, six months, prior year, other metrics, minimum VaR, segment
  revenue, decoy table). Found on the way and fixed: Hilton puts metrics in rows; Hyatt names
  the whole system "system-wide", not "worldwide"; JPM's note groups years as rows.
- Loan book: `loan_table_v2` also reads tables with classes as columns (JPM wholesale). The
  two models may read a segment at different levels (Sonnet: the wholesale "Loans retained"
  line, 846.8B; Haiku: its classes). Where one gives a single line and the other classes that
  add up to it, the single line is kept, since both models confirm its value; the classes,
  confirmed by one model only, are not stored. JPM total 1,463.8B as before the change.
- NIM relation: NIM = net interest income / average interest-earning assets, quarters
  annualized (x4), within 5% (JPM Q2 2026: 2.38% vs 2.40% reported, managed basis).
- Prompt injection: every user prompt has invisible characters made visible ("[U+200B]"); each
  source sits in a named block whose own closing tag is escaped inside it; list fields are one
  line; `prompts/untrusted_data_v1.md` is appended to every system prompt and its version is
  part of each stored prompt version. A quote-path answer for a table metric is ignored, so the
  cell check cannot be bypassed. Adversarial tests: hidden characters, a closing-tag breakout,
  an embedded instruction to report 999, a decoy table.
- Cost: the data rule changes every cache key, so the first run after this asks every task
  again (Claude); SecureGPT would have asked again anyway (other models).

**D-045 Board composition, review.**
- The documents stage downloads the latest proxy (DEF 14A). The new `governance` stage (after
  textmetrics; signals do not wait for it) has both models read the board passages; a fact is
  kept when its quote is in the proxy, its number (digits or words) or name is in the quote,
  and both models give the same value. Stored in `board_composition` (new pipeline table).
- Signals: CEO is also chair with no lead independent director; independent directors below
  two-thirds. Unknown facts never raise a signal. The facts enter the insight package as a
  source with the proxy's own words.
- `uw-pipeline review`: lists DISPUTED / OVERRULED signals and the latest identity errors with
  evidence and source link; `approve` / `reject` need `--reviewer`; a wrong or stale id gives
  the next step, not a traceback. Decisions in `review_decision` (new pipeline table); a
  rejected signal leaves the insight input and the DQ review queue.

**D-046 Debt, restatements, stability.**
- Total debt adds finance lease liabilities to the LongTermDebt family only (the
  ...AndCapitalLeaseObligations family already includes them). DQ: total debt >= long-term
  debt; Warning when total debt is the non-current-only fallback (Macy's has no current tag).
- Restatements (D-035): a quarter or year-to-date value restated after its fiscal year was
  reported keeps the year's basis until a filing restates the year as well; the quarters then
  add up to the year. Logged by the compute stage.
- `scripts/stability_check.py`: N live runs on a database copy with an empty cache, compared
  (Jira T2); `--faults` runs timeout, rate-limit and invalid-schema clients: all three failed
  closed (no exception, nothing stored, earlier values kept).
- Live result (Claude, MAR + JPM, 3 runs, about $0.93 each, 2026-10-14): board facts and all MAR
  values identical; no value ever differed in amount; 96 of 107 values stored in all three runs,
  11 (JPM AUM, LCR, VaR quarters, international revenue) withheld in some runs. The loan book's
  wholesale line was stored only when both models read it at the same level, which led to the
  roll-up rule in D-044. The SecureGPT run is still open (PLAN Phase 14).
- Not done: tables in the pipeline schema only, no app tables changed; deploy/sql/
  pipeline_schema_full.sql gains board_composition and review_decision.

## 2026-10-15

**D-047 Runware as a third LLM provider.**
- Asked for: DeepSeek V4 Flash through Runware. Kept: the securegpt and anthropic profiles,
  unchanged. Added: profile `runware` (`LLM_PROVIDER=runware`, `RUNWARE_API_KEY`).
- Roles: extractor `deepseek:v4@flash`; tie-breaker `anthropic:claude@haiku-5.5`; synthesis
  `anthropic:claude@sonnet-5.5`. Options were DeepSeek in all three roles (cheapest, but dual
  checks and signal votes would compare DeepSeek with itself) or comparison only; the
  extractor-only choice keeps every dual check two independent models.
- Client: `clients/runware.py`, Runware's OpenAI-compatible chat completions
  (`https://api.runware.ai/v1/chat/completions`, Bearer key), retries and error classes as the
  SecureGPT client; the OpenAI response handling is shared (`clients/openai_chat.py`).
  Account errors (no credit, bad key, unknown model) stop LLM use for the run.
- Smoke test 2026-10-15 (`scripts/runware_smoke.py`): strict JSON schema and
  `reasoning_effort` work on all three models; JSON mode is rejected for the Claude models,
  so every model uses the strict schema. Responses recorded in tests/fixtures/runware/.
- Open: Runware returns no cost on these calls, and its docs list no DeepSeek price. The
  prices in config/llm.yaml (DeepSeek $0.08 / $0.15 per million tokens from a third-party
  listing; Claude at Anthropic list prices) are to be checked against the Runware invoice.
- Switching a deployment to this profile asks every LLM task once again (the model is part of
  the cache key), about the cost of one cold run.
- Found in the first run (MAR, JPM, ENPH on a database copy, 2026-10-15): DeepSeek's reasoning
  tokens count toward max_tokens, so 8000 cut off 15 answers (now 32000; Sonnet synthesis also
  32000, largest output so far 11,433); and Runware sometimes leaks DeepSeek's reasoning into the
  content before the answer (`…</think>{answer}`), so the parser keeps only the text after the
  last `</think>`, which must still pass schema validation.
- Result vs the Claude profile (same 3 companies): signal votes 55 of 55 same outcome; text
  values identical where both have one (Runware 8 more, 1 fewer); JPM loan book stored by class,
  same total; board facts found the JPM lead director Claude missed. Cost of this cold run
  about $2.1 vs about $2.8 on the Claude profile: the saving comes from Haiku instead of Sonnet as
  tie-breaker; DeepSeek and Haiku cost about the same as extractor, because DeepSeek's reasoning
  output is 5-10 times Haiku's output.
- Experiment `runware_luna` (openai:gpt@6-luna in every role, no second opinion: the
  tie-breaker spec equals the extractor's, so dual checks read the cache). Same 3 companies:
  about $0.17 (third-party price), signal votes 55 of 55 same, but the cell check withheld 12
  wrong cells (MAR FY2025 RevPAR, occupancy, ADR missing) and 24% of key insights failed
  grounding; insights took 44 minutes (long answers past the 300 s timeout, retried). OpenAI
  models on Runware need `max_completion_tokens` (config `max_completion_tokens_prefixes`).

**D-049 Industry membership, tracked metrics and signals, by editing config/ (Phase 18, 2026-10-17).**
- User request: per company, which industry it belongs to; per industry, which metrics and
  insights it tracks; new metrics and insights; in YAML or JSON. "Insights" means what the
  pipeline already tracks (user clarification): the risk signals (8-K items, keyword rules,
  metric and board rules, judgment questions).
- Options: a separate override directory merged on top of config/ (built first: suits edits
  outside code review and ConfigMaps in deployment), or editing config/ directly. User decision:
  edit config/ directly; config changes go through PRs, so one place is simpler. The override
  layer was removed before commit.
- What was missing and is now in the existing files: `industry` + `industry_reason` per company
  (companies.yaml; checked before the SIC code); `exclude_metrics`, `exclude_signals` and
  `signal_params` per group (industry_groups.yaml); metric `thresholds` and `industries` on
  keyword and threshold rules (signal_rules.yaml). New metrics, concepts, keyword rules, groups
  and SIC mappings were already editable.
- An excluded metric that another tracked metric needs is computed as a hidden helper. A family
  id such as `8k_5.02` excludes its CEO/CFO variants. Signals are filtered after detection by
  rule id, so the stage's stale-signal deletion removes signals an industry stops tracking.
  Parameters are checked by field name and type.
- Not covered: judgment questions can be excluded by id but not defined (answer types live in
  code and the prompt); insight tasks are the same for every industry.
- App database: no schema change (user choice (a)): SIC_Preferences stays per SIC; a company in
  another industry shows the metrics it has data for.
- `uw-pipeline config check` validates config/ as a run does and lists company and industry
  settings; `config show --ticker X` prints a company's industry (and why), metrics, signals.
  Guide for editors: docs/CONFIGURATION.md.

## 2026-10-18

**D-050 D&O risk core layer: market data, insider trades, reporting quality, litigation (Phase 15).**
- Open questions settled at the start ("go ahead with phase 15", recommended options): insider
  trades from SEC Form 4, not AV INSIDER_TRANSACTIONS (authoritative, free, by CIK); stock drop
  = one day -10% or worse and at least 8 points below SPY; app database option A (no schema
  change: new metrics in MatrixData, new events through signals and insights into
  StrategicRisk); litigation (15e) stays in Phase 15.
- 15a market: AV TIME_SERIES_DAILY_ADJUSTED (full history, kept 7 years) and EARNINGS per
  company plus SPY, in `market_daily` (by symbol) and `earnings_report`; OVERVIEW not used.
  Market cap = quarter-end close x the cover page's shares outstanding (dei, dated up to 120 days
  after the period end), else diluted weighted-average shares. Metrics (derivation MARKET,
  metric kind `external`: written by another stage, skipped by compute): market_cap,
  volatility_90d (63 daily returns, annualized), max_drawdown_52w, eps_surprise_pct. Stage
  `market` runs after compute; Altman Z (compute) reads the stored market cap, so on a company's
  first run it appears one run later. Signals stock_drop and earnings_miss_drop (-8% within 3
  trading days of a miss). Acceptance: Ford 2024-07-25 -18.4% while SPY -0.5%, and the miss
  (EPS 0.47 vs 0.68) on 2024-07-24.
- 15b Form 4: originals only (4/A left out so no line counts twice); non-derivative lines. A
  sale is under a 10b5-1 plan when a footnote it cites says so; when no line cites one, the
  filing's 10b5-1 box applies to all lines (a filing ticks the box once for all its lines; the
  tax-withholding line of a plan sale is not a plan sale). Form 4s the company files as an owner
  of another issuer (JPMorgan as a 10% holder) are registered, not parsed. Metrics (derivation
  FORM4): insider_net_sales (officers' and directors' open-market sales minus purchases, USD)
  and officer_sell_pct (officers' shares sold / holdings before the sales). Signal
  insider_cluster_selling: 3 or more officers or directors selling on the open market outside a
  plan within 30 days; one signal per episode (30 days from its first sale, keyed on that date);
  Warning when a stock drop or earnings-miss drop follows within 90 days. Acceptance: every
  Form 4 EDGAR lists is registered (0 missing, 9 companies; 366 have derivative lines only); no
  sale left unflagged has 10b5-1 anywhere in its filing.
- 15c reporting quality: receivables and inventory days, accruals ratio, goodwill / equity and
  impairment, SBC / revenue, diluted share growth, debt maturities, near-term debt coverage,
  Altman Z (with total liabilities, or assets minus equity when that is not tagged). Coverage
  counts short-term investments (ShortTermInvestments, MarketableSecuritiesCurrent,
  OtherShortTermInvestments): without them Enphase (1.04B USD marketable securities) raised a
  false refinancing Warning. The plan's DQ warnings became threshold signals
  (near_term_refinancing > 1.0, altman_distress < 1.81) so they reach insights and the app; DQ
  stays about data correctness. Share counts are not derived (Q4 = FY - 9M gave 0 shares).
  Golden values: Marriott and Pfizer FY2025 10-K.
- 15d: 8-K 1.05 (Warning) and 1.02 (Info). EDGAR lists both 13Ds about the company and 13Ds it
  files about others (JPMorgan's about funds): only those whose index headers name the company
  as SUBJECT COMPANY are kept; the signal names the filer.
- 15e litigation: stage `legal` (after documents) reads the latest 10-K and the 10-Qs after it,
  passages chosen by keyword groups (45K characters), with both models. A matter is kept when
  its quote is in the filing and both models give it the same type; its start date and accrual
  only when they are in the quote. Across filings a matter is the same by type and name (generic
  legal words and the company's name ignored; all distinctive words of one name in the other),
  by the same full start date, or by agency and subject; across types only with nearly the same
  name (an investigation that became an enforcement action; the type follows the latest filing).
  Everything in a company's first run is its baseline; a matter first seen later (or named
  differently) is NEW and goes to `uw-pipeline review` (item type MATTER); a rejected matter
  leaves signals and insights. Signals for matters still disclosed (last 400 days) that began in
  the last 730 days, or are open with no date: securities class action and Wells notice
  Critical; derivative suit, investigation, enforcement Warning. All matters go to insights as a
  DOCUMENT source. Prompt written on MAR, F, LUV, M, ENPH; holdout PFE, JPM, LUMN: 41 matters, all
  types correct on reading; quotes grounded by construction. Readings left unpaired are mostly
  real ambiguity (cash-sweep suits as securities or consumer claims).
- 15f: `board_v2` adds the auditor fee table (each amount checked at any unit; a category a
  table splits into lines may be a sum of up to three numbers; the year must agree between the
  models), dual-class shares, and the CEO's start year (the name in the quote or in the 1,500
  characters before it). Chosen: columns on `board_composition` rather than a new `audit_facts`
  table (same document, same two-model check, one row per company). The auditor's PCAOB ID
  (dei:AuditorFirmId) decides auditor_change when both 10-Ks give it. Signals dual_class_shares
  (Info) and non_audit_fees (Warning at 50% or more of the auditor's fees). Result: fees 7 of 8
  proxies (JPM's are prose and the readings differ), dual class Ford only, CEO start year 5 of 8.
- 15h: dual_check on RevPAR, occupancy and ADR. Marriott's 15 values unchanged, dual_dropped 0;
  a disagreement is logged with both readings.
- 15i: companies of a stage run in parallel threads (jobs.yaml `parallel`), on server databases
  only (SQLite: one at a time). `LimitedClient` keeps per-model requests and tokens per minute
  across threads: a request reserves its input plus max_tokens, the unused part comes back, and a
  429 pauses that model for every worker. max_tokens per task per provider profile, set from
  the profile's own measured outputs. Not done: calls within one company stay serial and inside the
  stage's transaction (moving them out means restructuring each LLM stage); SecureGPT quotas are
  unknown (rate_limits commented out); the 10-minute cold-start target is not measured (needs a
  server database and a paid cold run).
- App: metric categories Reporting Quality and Market & Trading; risk types Market and Insider
  Trading; investigation words map to Regulatory & Policy; no app schema change. Pipeline
  tables: market_daily, earnings_report, insider_transaction, legal_matter; board_composition
  gains fee, dual-class and CEO columns.

**D-051 SecureGPT: gpt-6 models, api-version 2025-04-01-preview, 500K tokens per minute.**
- User decision (gateway's API catalogue, 2026-10-18): calls go to the deployment's chat
  completions with `api-version=2025-04-01-preview` (was 2024-10-21) and keep OpenAI structured
  outputs (`response_format` `json_schema`, strict) on every call. The gateway also lists the
  Responses API (`/providers/openai/responses`); not used, chat completions already give strict
  structured outputs and need no other change.
- Models (user decision): gpt-6-luna-2026-09-22 for routine calls (extractor: signal votes,
  first reads, news, scores) and gpt-6-sol-2026-09-22 as tie-breaker (second model of every
  dual check). Synthesis (insights, follow-ups, metric relevance) uses gpt-6-sol, as it used
  the tie-breaker model before. The gpt-5 deployments stay mapped in config/securegpt.yaml.
  The model is part of the cache key: the first run after the switch asks every LLM task again.
  Prices per million tokens, USD, entered in the EUR profile (user decisions): gpt-6-sol 2.00
  input / 10.00 output; gpt-6-luna 0.10 / 0.50 for now, a third-party list price, until
  SecureGPT's own is known.
- `rate_limits`: 500,000 tokens per minute for each deployment (gpt-6-luna, gpt-6-sol), as
  the user set. No requests-per-minute limit until the gateway team gives one; a 429 pauses
  that model for every worker (D-050). If the 500K is one quota shared by both deployments
  rather than each, halve the two values.
- `reasoning_effort` stays off (`send_reasoning_effort: false`) until
  `scripts/securegpt_smoke.py` on the work network shows the gateway passes it with the new
  version; without it the models use their default effort.


**D-052 S&P 100 coverage: companies, industry modules, coverage notes (Phase 16).**
- Companies: the S&P 100 as of 2026-09-21 (100 companies; GOOGL and GOOG are one CIK) plus
  the seven MVP companies outside it (MAR, LUV, F, M, LUMN, SKYD, ENPH): 107. CIKs pinned from
  SEC's ticker list on 2026-10-18. Who reviews the list each quarter is still open.
- Industry by company where SIC misleads (D-049 `industry`, 22 companies, each with its
  reason): e.g. 3M (SIC 3841, medical) is an industrial, Visa and Mastercard (7389, business
  services) are payments, CVS (5912, drug stores) is managed care, Amazon and Booking are
  internet, Danaher, Thermo Fisher and Abbott are medtech and tools.
- Predecessor registrants: `predecessor_ciks` is now read by the facts, documents and signals
  stages (each filing under the CIK it was filed with). ExxonMobil's listed parent changed
  on 2026-07-01 (holding-company reorganization; the latest 10-K and proxy are Exxon Mobil
  Corp's) and BlackRock's in 2024. A successor-only company (`history_start`, D-012: SKYD)
  still reads its own filings only.
- Industry groups (24): new SOFTWARE_INTERNET, HARDWARE, MEDTECH, MANAGED_CARE,
  CONSUMER_STAPLES, ENERGY, UTILITIES, AEROSPACE_DEFENSE, INDUSTRIALS, TRANSPORT, REIT and
  PAYMENTS, mapped by SIC ranges; non-bank financials (CREDIT, BROKER_DEALERS, INSURANCE) get
  FINANCIAL_CORE (returns, leverage, assets / equity) instead of liquidity and EBITDA.
- Module metrics, XBRL first: gross margin, R&D / revenue, deferred revenue and its growth,
  deposits and growth, unrealized securities losses / equity, assets / equity, net investment
  income, warranty reserve, pension and retiree-benefit liabilities (the funded status is
  tagged by plan only), regulatory assets, operating ratio. Text (MD&A and releases, both
  reads must agree): largest product revenue (and its share), medical cost ratio and members,
  uninsured deposits and CRE loans (and their shares), net flows, payments volume,
  comparable and organic sales growth, oil-equivalent production, postpaid phone net adds and
  churn, vehicle deliveries. Not done: customer concentration and oil reserves are in the
  notes and Item 2, which text extraction does not read; insurance gets net investment income
  only (Berkshire is the only S&P 100 insurer and reports no combined ratio). REIT FFO and
  occupancy, ARPU (telecom, media), utility storm costs and auto recalls are left for Phase 17:
  one or two S&P 100 companies each, and definitions differ by company.
- Count metrics (members, vehicles, barrels per day): metric unit `count`, extraction unit
  `count`, prompt `text_metrics_v3` (also: a growth rate is extracted when the definition asks
  for one, decreases negative).
- Fiscal calendars: quarter ends are learned from the company's own 10-Qs (their duration
  facts and `fp`); 12/12/12/16-week years (Costco, PepsiCo) put Q2 and Q3 up to three weeks
  before the month end. Duration classes widened (a 16-week quarter, 36-week year to date).
  Alpha Vantage dates such quarters at the month end; earnings match within 25 days.
- Tags added where the first run showed gaps: bank and broker net revenue (us-gaap:Revenues,
  RevenuesNetOfInterestExpense), utilities' operating revenues, interest expense (also
  InterestAndDebtExpense, and cash interest paid as the last fallback), separate depreciation
  and amortization, debt combinations (DebtCurrent with non-current long-term debt; LongTermDebt
  plus short-term borrowings; the combined debt total), buybacks from the equity statement,
  inventory net of progress billings (Boeing) and utility inventory, operating cash flow from
  continuing operations. EBITDA without an operating income line: pre-tax income + interest +
  D&A. A bank's net revenue where no total is tagged: a hidden helper of its own concepts, so
  bank metrics are not pulled into every company.
- Coverage notes: `groups:` notes for gaps a whole industry shares (and "*" for every
  company: goodwill impairment and buybacks are tagged only in periods with them; debt due
  within a year only when some is); company notes for the rest, each checked against the
  company's companyfacts. After the XBRL stages every gap is explained; text metrics follow
  their first extraction.
- Golden values (16e): two companies per new or changed module (24 tests), each value a fact
  of the company's latest 10-K or 10-Q.
- Restated years (first DQ review, user decision): a fourth quarter derived as year minus
  nine months carried the whole restatement when a filing between the two restated a fiscal
  year (19 of 107 companies have a restated year since 2022: spin-offs, discontinued
  operations, presentation changes). BAC's FY2025 10-K recast FY2024 revenue (101,887 ->
  105,856): our Q4 2025 was 31,180 and Q4 2024 29,316, against about 28,400 and 26,500 on
  the restated basis. Options: keep (wrong quarters, DQ quiet because they still add up);
  derive Q4 from the year as first reported (right as reported, but the stored restated
  year no longer matches its quarters: DQ errors); N/M. Chosen: N/M (no wrong value; the
  coverage report counts it, the quarters-vs-year check skips the year). N/M carries through:
  a formula with no usable form because an input is N/M, and a growth rate over an N/M
  period, are N/M too (before, they had no row and showed as coverage gaps). Years stay at
  their latest, restated value, so year-on-year growth compares like with like. And D-046
  gets an exception: when the year's own figure is already on the newer basis (a filing
  between the quarter's report and the year's restated a fiscal year), a quarter restated
  later matches its year and is kept (BAC Q1 and Q2 2025 now equal the releases).
- Release vs XBRL (first DQ review: 33 of 2,272 checks disagreed; the release quotes showed
  why). A release that prints a rounded headline ("net income of $1.8 billion" for 1,777M)
  now agrees when XBRL, rounded to the printed precision, gives that figure; an exact table
  figure must still agree within 0.5%. The preliminary definitions now ask for the exact
  figure in the income statement table and name the line ("Net income attributable to
  <company>", not "available to common shareholders", "profit", continuing operations or
  adjusted), matching us-gaap:NetIncomeLoss. A check whose XBRL value has since become N/M
  is dropped.
- BlackRock: total revenue is tagged as contract revenue; its us-gaap:Revenues facts (about
  11-13B a year) are not the total. Company override `company_overrides/BLK.yaml`.
- Short transactions (Phase 15i follow-up, user decision): insights and metricdetail commit
  their finished writes before each model call and each cached answer right after it
  (`cached_call(commit=True)`), so no transaction stays open while waiting for the model.
  A group of writes (retire the old set, insert the new one) still commits together. On a
  SQLite file these two stages now run companies in parallel (jobs.yaml `parallel`); SQLite
  files use WAL and a 60 s busy timeout. Measured in a test: two companies 1.3 s instead of
  2.3 s. The other LLM stages still hold one transaction per company (Phase 17d), and calls
  within a company stay serial (behavioral and strategic could overlap: ~20%).
- Ratios over text metrics (top product, uninsured deposit and CRE loan shares) are computed by
  `compute` from the text values stored earlier, as Altman Z uses the stored market cap: a new
  text value gets its ratio on the next run. Only metrics of kind `text` are read back; a
  metric with XBRL and a text fallback (CET1) keeps its XBRL values as formula input.
- Claude calls get a 10-minute deadline, retries included (`ANTHROPIC_TIMEOUT_SEC`; the
  SDK's timeout bounds each socket read, not the call). A call past it fails as one item
  (retried next run); later calls use a new client. The stalls that prompted it (BAC legal
  at 15:38, INTC legal at 16:34) began the second the laptop went into idle sleep (pmset
  log): the process freezes, the connection dies, and timers count only awake time. Long
  local runs go under `caffeinate -i`; servers don't sleep.
- Development LLM runs use Claude Haiku 5.5 only (user decision; profile `anthropic_haiku`):
  dual checks then compare Haiku with itself at two efforts, less independent than with Sonnet.
- Open: market cap for multi-class companies (Alphabet, Visa, Berkshire) needs per-class
  share counts; total loans for Wells Fargo and American Express are not tagged with standard
  tags (company concepts needed); the quarterly list review owner.

- Closing checks (2026-10-20):
  - Coverage: a formula over a text metric (product, uninsured-deposit and CRE shares) now gets
    the text target (4 quarters), as the text it is built on covers about 400 days. The rest got
    notes: capital ratios per industry (BANKS: older periods only where XBRL ratios are mapped,
    JPM), and company notes naming the cause. 0 unexplained gaps (3,625 OK, 525 explained).
  - Found while writing them: for 5 of 8 banks no MD&A text is read. Wells Fargo, US Bancorp
    and BNY put the MD&A in the annual report (Exhibit 13; Item 7 only refers to it, and Wells
    Fargo's 10-Q Item 2 is a table of contents); for Citi and Morgan Stanley the section splitter
    finds no Item 7 / Item 2. Their text metrics (LCR, NIM, NPL, uninsured deposits, CRE, net
    flows) are missing for that reason; fix in Phase 17d (read Exhibit 13, section rules).
  - Text-metric holdout, 2 companies per metric (32 values): all right for their period. Weak
    evidence in two (COF CRE loans, GM deliveries: the quote is a table's total line without its
    label); Ford's "deliveries" are wholesale units; Costco's comparable sales are as reported
    (with gasoline and FX), BAC's margin is net interest yield on an FTE basis.
  - audit-insights checked only 10-K/10-Q section text, so proxy facts, litigation matters
    (rendered for the model with the filing's words) and loan rows read by table cell ("row |
    value") were reported as not found. It now checks them against what the model saw: 1,580 of
    1,580 snippets in a 600-insight sample, 14 of 14 loan rows.
  - stability_check --faults (JPM, MAR, UNH, COST, XOM): time-out, rate limit and invalid
    output each fail closed; nothing stored changes.
  - SQL Server (`scripts/test_mssql.sh --all`): 413 passed, including the KeyTakeaways table.


**D-053 Key Takeaway: 2–3 bullets for professional lines above the Key Insights (2026-10-20).**
- User request: a Key Takeaway box on the company page (prototype: "Key Takeaway", above "Key
  Insights Summary"): from all of the company's current results, the 2–3 points that matter most
  for professional insurance, without links.
- Generation: a sixth step of the insights stage, `key_takeaways_v1` on the synthesis model,
  after the follow-up questions. Input: metrics, signals and the current key, strategic/risk and
  behavioral insights. Scope (user decision): D&O first (securities and derivative claims,
  regulatory, governance and insider selling, distress, M&A), then E&O, cyber and employment
  practices where the input shows them. Each bullet is one sentence with its fact and date.
- Same checks as the other insights though nothing is shown but the text: schema; quotes of the
  cited items must be found and every number must come from the input, else the bullet is
  dropped; at most 3 kept; versioned by input hash (a new version only when an input changes).
  The evidence is stored in `insight_evidence` (citations of key insights resolve to their own
  sources) for `audit-insights`, and the link-coverage DQ check skips takeaways.
- App storage, options: (A) a new table `KeyTakeaways` (CompanyId, Ordinal, Takeaway,
  UpdatedDate, isActive); (B) `KeyInsights` with a "Takeaway" type, which the Key Insights cards
  would have to filter out; (C) a text column on `Company`, without versions. Chosen (user
  decision): A, versioned like KeyInsights. The BFF must add the read; recorded in
  `docs/schema-contract.md`, DDL in `deploy/sql/app_schema_changes.sql`.
- Trial on MAR, JPM, ENPH: 3 of 3 bullets kept for each, all specific and dated (e.g. ENPH:
  three open securities class actions, the newest filed 2026-02-17). Some run past the 35-word
  target. Cost: one call per company with changed inputs (about $0.01 on Haiku).


**D-054 Hide the Phase 15 metrics beyond the BRD (2026-10-20).**
- User request: the metrics Phase 15 added beyond the BRD (Professional 360 MVP: financial,
  liquidity, leverage, operational, FI trends, loan portfolio) are hidden for now and kept out
  of key insights, key takeaways and the other LLM outputs.
- Scope: the three Phase 15 metric sets, 21 metrics: REPORTING_QUALITY (receivable and
  inventory days, accruals ratio, debt maturities 1-5+ years, near-term debt coverage, Altman Z),
  MARKET (market cap, 90-day volatility, 52-week drawdown, EPS surprise, insider net sales,
  officer sell %), CAPITAL_QUALITY (goodwill / equity, goodwill impairment, SBC / revenue,
  diluted share growth).
- How: `hidden_metric_sets` in `config/metrics.yaml`. The metrics are still computed and stored
  (the market and documents stages still write theirs), so showing them again means deleting
  the line, with no backfill. Hidden means: not published (MatrixData, CompanyMetricDetail and
  the SIC's SIC_Preferences rows are removed; the metric and category definitions stay), not in
  any LLM input (the package takes only displayed or insight-input metrics), not in the coverage
  report.
- Signals (user decision): threshold signals on a hidden metric are not raised, as their text
  quotes the value: near_term_refinancing and altman_distress (13 Altman signals removed). The
  event signals stay (stock drop, earnings-miss drop, insider cluster selling, 8-K 1.05 / 1.02,
  13D, litigation): they fit the BRD's Strategic & Risk categories.
- BFF: no change if it builds a company's metric list from SIC_Preferences or MatrixData and hides
  categories without metrics; "Market & Trading" and "Reporting Quality" are now empty.


**D-055 Phase 17 scope decisions and the company universe build (2026-10-20).**
- User decisions on the open Phase 17 questions:
  - SPACs (SIC 6770, 174 companies) are not in underwriting scope: no SPAC_SHELL group, and the
    universe excludes them. Coverage of the remaining universe stays about 98%.
  - Clinical-stage trait: life-sciences companies with latest fiscal-year revenue below USD 10M.
  - New units (ounces, tonnes, pounds, months) are displayed and get year-over-year growth like
    other metrics (same unit in both years); no threshold signals on them except cash runway.
  - Order: steps 17a/17a′ (universe and sample), the groups that only need SIC mapping, then the
    six groups with their own metric modules; review after those.
- Universe rule (口径 A without SPACs): CIKs listed on NYSE, Nasdaq or Cboe in SEC's
  `company_tickers_exchange.json` whose latest annual report since 2024-07-01 is a 10-K (or
  10-KT). Excluded with a reason in the report: foreign issuers (20-F / 40-F), funds (N-CSR), no
  annual report since the cut-off, SPACs (6770), and exchange-traded products: SIC 6221 or 6199
  with "Trust", "Fund" or "ETF" in the name (gold, oil, currency and crypto trusts; operating
  companies under 6221 such as Uranium Royalty Corp. stay).
- Revenue for ranking: XBRL frames (CY of the latest full year, the previous year as fallback),
  the largest of the revenue tags; banks without a revenue tag rank by interest and dividend
  income (marked in the sample list). The sample is a review list, not a run-time choice: the
  chosen companies are pinned by CIK in `config/companies.yaml`.
- Raw landing, options: (a) land all ~6,000 submissions responses (1–2 GB per build, quarterly);
  (b) land the ticker list and frames as-is, and one summary line per registrant (the fields the
  rule reads) instead of each submissions response; (c) SEC's bulk submissions.zip (about
  1.5 GB). Chosen: (b). The build is replayable from the landed files, and local disk is short.
- SIC ranges in `industry_groups.yaml` must not overlap (checked when the file loads), so the
  order of the list no longer decides a company's group; exact `sic` codes still take
  precedence over ranges.
- Two of PLAN 17b's groups are split, as their companies need different module metrics (seen
  in the companies' XBRL): MINING (1000–1499 without oil and gas: reserves, ounces and pounds,
  asset retirement obligations) and METALS (3310–3399: tons shipped, mill utilization);
  HOMEBUILDERS (1520–1531: land, closings, cancellations, inventory write-downs) and
  CONSTRUCTION_ENGINEERING (1540–1799: backlog as remaining performance obligations, core
  metrics only). 39 groups in all.
- Casino operators file under SIC 7011 with the hotels; in the sample they get
  `industry: LEISURE_GAMING` (gaming hold and visitation, not RevPAR). Outside the sample they
  stay in HOTELS (PLAN 17a′: no per-company settings after the freeze).
- Not mapped (PLAN 17, remaining 2%): water transportation (4400–4499), which is mostly cruise
  lines (Carnival, Royal Caribbean, Norwegian); they fall under 17g's LLM choice until a module
  exists.
- The new groups' module metrics (user decision, 2026-10-21) are treated like Phase 16's
  industry modules: shown under Operational Metrics and used by key insights, key takeaways and
  the other LLM outputs. The Phase 15 sets hidden by D-054 stay hidden for the new groups too
  (`hidden_metric_sets` applies to every group).
- Sample review decisions are kept in `config/universe.yaml` (read only by the universe script):
  `industry` for registrants not yet in companies.yaml whose SIC misleads (with the reason,
  carried into companies.yaml when a company is added) and `sample_skip` for registrants whose
  ranking revenue is not comparable (StoneX, Galaxy: gross commodity and digital-asset sales).
  A year's revenue more than 20 times the year before is taken as a tagging error and the
  earlier year ranks the company (Tigo Energy tagged $103.5B for CY2025).

**D-056 Phase 17 industry modules (2026-10-21).**
- Seven module sets, XBRL first, at most three text metrics per group (PLAN 17b), designed from
  the sample companies' own XBRL (tags and dimensions each of them files):

  | Group | XBRL | Text (two reads must agree) |
  |---|---|---|
  | CHEMICALS_MATERIALS | environmental remediation reserve, capex / D&A | sales change from volume, from price |
  | MINING | asset retirement obligations, environmental reserve, capex / D&A | gold produced (oz), copper produced (lb), aggregates shipped (tons) |
  | METALS | environmental reserve, capex / D&A | tons shipped, average selling price per ton (+ capacity utilization) |
  | HOMEBUILDERS | homebuilding inventory, land and lots, land / equity, inventory impairments | homes closed, backlog value, cancellation rate |
  | HEALTHCARE_SERVICES | labor cost / revenue | same-facility revenue growth, same-facility volume growth, admissions |
  | LEISURE_GAMING | casino revenue / revenue (ProductOrServiceAxis = CasinoMember) | attendance |
  | RESTAURANTS | labor cost / revenue, franchise revenue / revenue (FranchisorDisclosureAxis) | comparable sales, restaurants system-wide, franchised share of restaurants |

- Left out of the plan's list, with the reason:
  - All-in sustaining cost (gold): a fourth text metric for MINING, whose sample is two copper
    miners, one gold miner and two aggregates producers; production volumes cover all five.
  - Medicare and Medicaid share of revenue: filers give each payer separately (HCA: Medicare,
    managed Medicare, Medicaid, managed Medicaid), so a share needs a sum, and text metrics
    take numbers as printed, never computed.
  - Average unit volume (restaurants): two of five sample companies report it.
  - Revenue by property or region (gaming): segment members differ by company; casino share of
    revenue uses the standard member instead.
- Restaurant counts are text, not XBRL: us-gaap:NumberOfRestaurants comes in units
  Restaurant, Store or Location (McDonald's uses all three), and a concept reads one unit.
- Land and lots = homebuilding inventory − homes under construction, then the land tags:
  PulteGroup tags only raw land ($0.57B) with the land tag while its land under development
  ($7.4B) is the untagged remainder; D.R. Horton and Toll Brothers agree within 10–20% on
  both definitions (the difference also holds model homes).
- Company overrides (sample companies, PLAN 17a′): McDonald's and Yum China tag franchise
  revenue under their own tag and member.
- Text-metric holdouts (PLAN 17e), fixed before the first run: definitions are tuned on the
  development companies only; the holdouts are read once the definitions are final.

  | Metrics | Development | Holdout |
  |---|---|---|
  | volume and price effect | DOW, LYB, LIN | CRH, SW |
  | gold produced | NEM | FCX (gold by-product) |
  | copper produced | FCX | SCCO, NEM |
  | aggregates shipped | VMC | MLM |
  | tons shipped, selling price per ton | NUE, STLD | CLF, AA |
  | homes closed, backlog, cancellation rate | DHI, LEN, PHM | TOL, NVR |
  | same-facility revenue and volume growth, admissions | HCA, THC | UHS, DVA, LH (volume) |
  | attendance | LYV | none: no other sample company reports it |
  | restaurants, franchised share | MCD, SBUX, CMG | DRI, YUMC |

- Holdout result (2026-10-21, definitions final; runs 125–126, Haiku $1.21): every holdout
  value kept matches its filing, about 30 values across FCX gold, SCCO copper, MLM aggregates,
  CLF and AA shipments and prices, TOL and NVR closings, backlog and cancellations, UHS
  same-facility revenue, DVA treatments, YUMC restaurants and franchised share (quotes 100%
  found, values 100% right; PLAN 17e asks 100% / 95%). No value where the filing gives no
  company total: CRH (volume and price per product line), SW (none), UHS admissions and volume
  (acute care and behavioral health apart; the second read refused the extractor's segment
  figure), CLF utilization (not reported), NEM copper (tonnes). Misses, left as found
  (holdouts are not tuned on): Labcorp's "organic volume increased by 1.8%" was not read;
  Darden gives its restaurants in parts (2,218 owned plus franchised), not a system total.

- Core change found on the sample: `RevenueFromContractWithCustomerIncludingAssessedTax` is the
  last revenue candidate; it is Valero's only total and Wynn Resorts' since 2019 (both had no
  current revenue). It fills only periods where no earlier candidate is tagged.

**D-057 Azure credentials from a Databricks secret scope (2026-10-21).**
- User request: connect to Azure SQL the way the team's loss-run project does on Databricks
  (`loss_run_integration`, data_io/sql_connect.py and config/main_config.cfg): an
  `mssql+pyodbc` URL with the service principal as user name and password,
  `ActiveDirectoryServicePrincipal`, `ODBC Driver 17 for SQL Server`, and the service
  principal's id and secret read from a Databricks secret scope whose key names are in config
  per environment (`username_key`, `password_key`; for ADLS `client_id_key`,
  `client_secret_key`).
- Options for where credentials come from: (a) environment variables only (until now); (b) the
  secret scope only; (c) environment variables when set, else the secret scope named in
  config/azure.yaml. Chosen: (c). Local runs, containers and tests keep using environment
  variables; a Databricks job needs no extra setup. Only key names are in config, never values;
  secrets are not logged (the log names the scope and key).
- Driver: `ODBC Driver 17 for SQL Server`, which the team's clusters have (was 18).
- `TrustServerCertificate`: the loss-run project sets `yes` (the server certificate is not
  checked). Kept `no` (checked, D-040) as `trust_server_certificate: false` in azure.yaml;
  set it to true only if the cluster cannot verify the certificate.
- The Databricks copy notebook connects the same way (pyodbc, secret-scope keys from
  config/azure.yaml) instead of Spark JDBC with an Azure AD token: no tenant id or
  azure-identity, and no single-user cluster requirement.
- Tuned on development companies after the first run (2026-10-21): the same-facility metrics
  name Tenet's terms (same-hospital, adjusted admissions, net patient service revenues); capacity
  utilization (Phase 16, manufacturing trait) also reads steel mill utilization and operating
  rates (Nucor 83%, Steel Dynamics 90%), which re-reads it once for the other manufacturers.

**D-058 Clinical-stage companies (Phase 17c, 2026-10-21).**
- Which companies: life-sciences SIC codes (2833–2836, 8731–8734, 3826, 3841–3851) with latest
  fiscal-year revenue under USD 10M or none (D-055 threshold); about 390 of the universe.
  `scripts/build_universe.py` lists them; a person confirms each, and the result is pinned in
  companies.yaml as a company trait, reviewed with the quarterly universe rebuild. Never decided
  at run time.
- How: `traits: [clinical_stage]` on a company (new; traits were per industry group). Options
  for the metrics that do not fit (R&D / revenue, gross margin, largest product share): (a) a
  separate CLINICAL group, which would lose the SIC group's other settings; (b) the trait adds
  its sets and drops the metrics listed under `trait_exclude_metrics`. Chosen: (b); the company
  keeps its PHARMA or MEDTECH group.
- Metrics (XBRL, set CLINICAL_STAGE): cash runway in months = (cash + short-term investments) /
  (operating cash outflow of the last four quarters / 12), N/M when operating cash flow is not
  negative; R&D / total operating expenses. When the four quarters are not all available, or
  one is N/M (a restated year, Xenon's Q4 2025), the latest quarter's burn is used. The four-quarter sum is a new formula suffix,
  `x_ttm` (the four quarters ending at the period; the year itself for a fiscal year), like
  `x_py`. Unit: months (D-055).
- Signal: cash runway under 12 months (4 quarters), a threshold rule. Going concern and
  dilution signals exist already.
- Pipeline facts (LLM stage `clinical`, after `legal`): the lead programs from the latest 10-K
  (Item 1) and 10-Q (MD&A): name, indication, phase, next milestone and its timing as written.
  Both models read; a program is kept when its quote is in the filing, names the program and the
  phase, and both models give it the same phase; a milestone only when both give the same
  timing. Stored in `clinical_program` (one row per program, `is_current`); an insight input
  like the board facts, not published to MatrixData.
- Development set: the 20 clinical-stage candidates with the largest R&D expense (XBRL frames,
  latest year), reviewed and added to companies.yaml; PLAN 17e checks revenue judgment and
  pipeline facts on them.
- Found while building it: a second `short_term_investments` concept in concept_map.yaml
  silently replaced the Phase 15c one (plain YAML keeps the later key), which a golden test
  caught. Config files are now read with a loader that refuses a key given twice
  (`uw_pipeline/yaml_load.py`); the two biotech tags were added to the original concept.
- Result on the 20 (runs 127–130, 2026-10-21; `clinical` on Haiku $0.29): all 20 are
  pre-revenue (latest year at most $8.3M, nine tag none); cash runway at Q2 2026 from 6.3
  months (Biohaven) to 77 (CRISPR), two under 12 (Biohaven, Replimune: `short_cash_runway`).
  85 current programs, 52 with a dated milestone (Cogent's PDUFA date December 30, 2026;
  Vaxcyte's VAX-31 adult Phase 3 topline data in Q4 2026; Xenon's azetukalner NDA in Q3
  2026); seven companies checked against their filings. Tuned on them: a candidate named in
  the heading just before a quote counts (Celldex states barzolvolimab once per section), and
  indications are compared without their parenthetical detail (one CDX-622 program, not two).
  Golden values: Viking (four quarters) and Xenon (latest-quarter burn).

**D-059 Companies outside every industry group: the LLM picks one (Phase 17g, 2026-10-21).**
- Applies to a company whose SIC code maps to no group and that has no `industry` in
  companies.yaml (no SIC, BDCs, shipping, cruise lines). Foreign issuers stay out of scope.
- What the model chooses, options: (a) any combination of metric sets; (b) one existing
  industry group. Chosen: (b). A group carries its sets together with its traits, excluded
  metrics, signal settings and `is_financial`, which a set combination would not; the plan's
  bound holds either way (only existing metrics, no new ones, no formula changes).
- Input: the business section of the latest 10-K (Item 1; the first passages of the filing
  when it has no Item 1) and the SIC description; the list of groups with their label and the
  names of the metrics their module sets add. Output: the group code (an enum of the configured
  codes, so an unknown code fails schema validation), a verbatim quote from the business
  section that supports it (must be found), and a one-sentence reason.
- Both models choose (`industry_choice_v1`). The same group: AGREED and used. Different:
  the tie-breaker's group is used, DISPUTED, and the choice waits in `uw-pipeline review` (type
  INDUSTRY); a rejected choice is not used (the company stays in the default group until someone
  sets `industry`). Calls are cached by input hash, so later runs reuse the choice until the
  business section changes.
- Where: a new `industry` stage after `documents` stores the choice in `industry_choice` and
  sets the company's group; it recomputes that company's metrics in the same run (its facts
  are those of every company: instance-only module concepts follow on the next run). The
  `company` stage keeps a stored, usable choice when the SIC code gives no group.
- App: the metric methodology text of such a company says the metrics were chosen by AI from
  the business description (no new field, plan option A).
- Trial (run 132, 2026-10-21, Haiku $0.01) on the largest company of each kind outside the
  groups: both models chose the same group for all six, each supported by its 10-K: Ares
  Capital (BDC, no SIC) CREDIT, Carnival (4400) HOTELS, Dolby (6794) SOFTWARE_INTERNET, Kirby
  (4400) TRANSPORT, SSR Mining (6795) MINING, Texas Pacific Land (6792) ENERGY; their metrics
  were recomputed in the same run (SSR Mining: retirement obligations, capex / D&A).

**D-060 Bank MD&A: the annual report in Exhibit 13, and heading rules (Phase 17d, 2026-10-21).**
- Found: Wells Fargo, U.S. Bancorp and BNY file the 10-K as a short wrapper (cover, Item 1,
  a cross-reference index); MD&A, risk factors, controls and the financial statements are in
  the annual report, filed as an `EX-13` / `EX-13.1` document (file names like
  `wfc-20251231.htm` or `usb-20251231_d2.htm`, so the type comes from the filing index, not
  the name). In the wrapper, Item 7 is a 300-character cross-reference. Bank of America's,
  BNY's, Citi's, Morgan Stanley's, U.S. Bancorp's and Wells Fargo's 10-Qs and Citi's and Morgan
  Stanley's 10-Ks use their own headings; their 10-Q MD&A was missing or a stub.
- Which document is the 10-K's primary text, options: (a) keep the wrapper primary and add the
  annual report as a second role every reader must learn; (b) the annual report becomes the
  filing's PRIMARY document and the wrapper is kept as role `10-K-WRAPPER`. Chosen: (b). Every
  reader (signals, text metrics with their table reads, legal, citations) already uses the
  PRIMARY document of a filing, and the annual report is where the cited text is.
- When: a 10-K whose MD&A (10K-7) is missing or shorter than 3,000 characters, and whose
  filing index lists an EX-13 document. Its sections come from the item split plus the
  company's `sections:` rules; the wrapper's sections the annual report does not have (Item 1,
  Part III) are kept with it, after its text. Same for the older 10-Ks read for MD&A history.
- Heading rules for BAC, BNY, C, MS, USB, WFC in `company_overrides/` (as JPM's). Two rule
  changes: a rule's section is the longest block between a start and an end line (a table of
  contents lists the same headings; of several starts reaching the same end the first counts,
  since page headers repeat the start heading, as JPMorgan's do); a section the item split found at the same place under another code is
  dropped (Bank of America's 10-Q puts MD&A before the "Part I" heading, so the split filed it
  as Part II Item 2). Rules apply only to their own form (10K- / 10Q- codes).
- Citi's and Morgan Stanley's risk management discussion (credit, liquidity, market risk:
  nonperforming loans) follows the MD&A under its own heading; their MD&A rule runs through it,
  so the MD&A text holds what the other banks' MD&A holds.
- Result (runs 133–135, 2026-10-21; `textmetrics` on Haiku, 1.67M input / 0.29M output
  tokens): every 10-K and 10-Q of the six banks now has its MD&A (108k–462k characters, was
  0–776). Text metrics gained: Citi NIM, CET1, Tier 1, total capital, LCR (none before); U.S.
  Bancorp and BNY the capital ratios and LCR; Wells Fargo the capital ratios, CRE loans and
  uninsured deposits. Checked against their quotes: Wells Fargo CET1 10.61% at 2025-12-31,
  10.29% at Q1 2026, 10.26% at Q2 2026; U.S. Bancorp CET1 10.8% at 2025-12-31; Citi NIM 2.47%
  for 2025, 2.49% for Q4 2025, 2.46% / 2.54% for Q1 / Q2 2026.
- Left for 17e: no NPL ratio for Citi, U.S. Bancorp and BNY (their filings do not state
  nonaccrual loans to total loans directly, which the definition requires; U.S. Bancorp states
  nonperforming assets to loans plus other real estate); no Item 3 section for Citi and Morgan
  Stanley (legal proceedings are in the notes, which the legal stage reads from the whole
  document); BNY's annual report has no auditor tenure sentence, so no auditor's report section.
  Morgan Stanley is a broker-dealer: its text metrics are those of BROKER_DEALERS.

**D-061 Short transactions for the other LLM stages (Phase 17d, 2026-10-21).**
- Stages: textmetrics, governance, legal, clinical, industry, signals, scores and news join
  insights and metricdetail (D-052): no transaction is open while a model answers, so on a
  SQLite file their companies run in parallel too, and on Azure SQL no lock is held for minutes.
- Checked per stage that no group of writes spans a model call: governance, legal, clinical,
  industry and signals write only after all their calls; scores writes each news batch, and
  each transcript's tone with its flags, after that item's calls; news commits pruned
  articles, then the new articles (unjudged articles are judged on a later run if the
  relevance call fails); textmetrics writes its metric values (upsert and stale deletes) as one
  group after all extraction calls, then the loan book after its own calls.
- How, options: (a) a `commit` argument through every helper that calls the model (as
  insights' `TaskRunner`); (b) the stage opens its connection with `short_transactions(engine)`,
  which marks it, and `cached_call` commits around the call on a marked connection. Chosen:
  (b): one change per stage instead of a dozen signatures, and a helper called inside an
  ordinary transaction (tests, other callers) keeps that transaction whole.
- A company that fails partway keeps its committed groups and its cached answers; the next run
  completes it without new calls for what was answered.
- Tests (`tests/test_short_transactions.py`): for each stage but news, two companies in
  parallel on a SQLite file store the same rows as one after the other, and during every model
  call another connection gets the write lock at once (a stage switched back to one long
  transaction fails this). Live (run 136, 2026-10-21): signals for eight companies, four
  workers on `data/uw.db`, 23 s, no lock errors.

**D-062 Model calls within one company in parallel (Phase 17d, 2026-10-21).**
- Where: insights' behavioral and strategic-risk syntheses (independent: both prompts are
  built from the package before either runs); textmetrics' reads of all documents, each with
  its dual-check read and table reads, and the loan book (no request depends on another
  answer: the second read is made whatever the first returns).
- How, options: (a) run the stage's steps in threads, each with its own connection; (b) a
  pre-fetch: build the company's requests first, ask the model for those not in `llm_call` a few
  at a time in threads that do not touch the database, store the answers on the caller's
  connection, then run the stage's code unchanged: every call finds its answer in the cache.
  Chosen: (b). A connection cannot be shared between threads, and (a) would split one company's
  writes across connections; with (b) validation, grounding, dual checks and writes stay
  sequential and as tested.
- textmetrics builds its requests by running its document loop once in a planning mode in
  which the extractor records each request instead of calling (parsed tables are kept for the
  real pass). A pre-fetched call that fails is not stored; the call that follows asks again and
  records the error, as before. An answer that fails validation is stored as such (its tokens
  count) and asked again by that call.
- How many at a time: `company_calls` in config/llm.yaml (3); the per-model rate limits
  (`rate_limits`) still apply across all threads, and the daily budget is checked first.
- Tests (`tests/test_prefetch.py`): requests asked three at a time, duplicates and cached ones
  not asked, a failed call not stored and asked again, an invalid answer kept for its tokens
  and asked again; textmetrics on three releases stores the same values with one or three
  calls at a time. The insights parallel test now sees four calls at once (two companies, two
  syntheses each). Live (run 137, 2026-10-21): textmetrics for the six banks and JPMorgan from
  the cache: no model calls, the stored values unchanged.

**D-064 Daily copy from SQLite to Azure SQL: sync by primary key (2026-10-24).**
- Need: the pipeline runs on SQLite; Azure SQL, read by the BFF, must get each run's results
  without a full reload. A full reload (`clear_target`, `load`) breaks once the BFF's user
  tables (favorites, flags, metric choices) point at application rows.
- Options: (a) the pipeline writes Azure SQL directly (its publish stage is already
  incremental, `pipeline_publish_map`); (b) keep SQLite and sync the file into Azure SQL after
  each run. User decision (2026-10-24): (b).
- How: `copy_sqlite_to_azure` gets `mode = sync`: each table staged as text, then one `MERGE`
  by primary key (insert new rows with their ids, update rows whose columns differ by
  `EXCEPT`, delete rows gone from the file), constraints off meanwhile and re-checked after,
  row counts compared. Values are converted as `load` converts them (decimals through FLOAT,
  times through DATETIMEOFFSET, then the target type) so unchanged rows compare equal and are
  not rewritten. `keep_columns` (default `Company.logo`) are columns the application writes:
  inserted for new rows, never updated.
- Conditions: the target was filled by `load` from the same pipeline (same ids) and nothing
  else writes these tables. The pipeline never deletes companies or versioned content (old
  versions get `isActive = 0`), which the BFF's user tables refer to.
- Application tables (2026-10-26): every mode copies only the tables the repo's DDL for the
  database creates; `User*` and any other table are never copied, synced or cleared, even when
  the SQLite file has one of that name (a local `app.db` from the BFF prototype does), so the
  users' data in Azure SQL cannot be overwritten.

**D-065 Key Insights: summary at most 180 characters (2026-10-24).**
- Need (user, 2026-10-24): the summary of a Key Insight card fits in 180 characters. Before:
  "two or three sentences"; of 1,052 current summaries 854 were longer (average 228
  characters, longest 420). Key Takeaways keep their length and at most 3 per company (a
  100-character limit on them was tried the same day and withdrawn at the user's request).
- How: prompt `key_insights_v2` asks for one or two sentences of at most 180 characters
  (counting spaces) and says a longer summary is not shown; `config/insights.yaml`
  `text_limits.key_insights: 180`. The insights stage drops a grounded key insight whose
  summary is over the limit (logged as a dropped example), never cuts it. The prompt version
  is in the input hash, so every company's key insights are generated again on the next run,
  and with them the follow-up questions and takeaways that use them (about 3 synthesis calls
  per company); behavioral and strategic insights stay cached.
- Result (MAR, JPM, ENPH, 2026-10-24): 10 key insights each, summaries of 94-169 characters,
  none dropped. tests/golden re-exported.

**D-066 A company's insight sets change together or not at all (2026-10-25).**
- Found in nightly run 144 (107 companies, Phase 16 code): the Mac slept mid-run and four
  calls timed out. For M, LUMN and SKYD the strategic risks were stored as a new version while
  the key insights gave no output and kept their previous version, which linked to the risks
  just retired: 21 `link_integrity` DQ errors (Related lists pointing at retired items). A
  timeout, a content filter or an unusable answer would do the same.
- Options: (1) store a company's new sets only when every set that had to change has output,
  otherwise keep all previous versions; (2) keep the previous version of the failed set and
  re-point its links to the new versions. User decision (2026-10-25): (1); no mix of versions.
- How: each new set is written as a staged version (`is_current = False`); at the end of the
  company the staged sets become current together (previous versions retired) when no set gave
  "no output", else the staged rows and what hangs on them (evidence, links, metric links,
  question sources) are deleted. They were never current, so nothing published changes. An
  error part way through does the same. The model answers are cached, so the next run retries
  at no cost. The task detail says `stored=none, previous versions kept (no output: ...)`.
- Trade-off: a set that keeps failing (e.g. a request too large) holds the company's whole
  insight update back; the task detail and the logs name the set.

**D-067 Company logos: files kept in the repo, published by the pipeline (2026-10-26).**
- `Company.logo` was NULL for every company: the application was meant to fill it and never
  did. The front end needs something it can use as an `<img src>` without another request.
- Options: format (1) base64 data URI, (2) an image URL (hotlinking a third party or hosting
  the files); maintenance (a) the pipeline finds logos at run time, (b) logo files kept by hand
  in the repo, (c) the application team. User decisions (2026-10-26): (1); first (a), then (b)
  the same day, because the corporate network that runs the pipeline does not reach Wikimedia.
- How: `config/logos/<TICKER>.png|jpg|gif|webp` (at most 200 KB, about 250 px wide; SVG is
  not accepted: it can carry scripts). `publish` writes each company's file as
  `data:<type>;base64,...` to `Company.logo` (NULL without a file); `config check` rejects
  files without a configured ticker, of the wrong type or too large. `Company.logo` becomes
  `NVARCHAR(MAX)` (`app_schema_changes.sql` widens a deployed column; run it before the next
  sync, or the longer values do not fit). The copy notebook's `keep_columns` default is now
  empty: the pipeline owns the column.
- Filling the folder: `scripts/fetch_logos.py`, on a network that reaches Wikimedia, fetches
  logos for companies without a file and writes `config/logos/SOURCES.yaml` (where each came
  from) and a review sheet `data/reports/logos.html`. Order: the `logo` of the infobox of the
  English Wikipedia article linked from the Wikidata item with the company's CIK (or a
  predecessor's); the item's Wikidata logo on Commons; the website's icon (Google's favicon
  service, at least 32 px). Matching is by CIK only: searching by ticker or name found wrong
  companies (ARCC -> Ares Management, DYN -> Dynegy). `config/logos.yaml` holds the
  corrections checked by hand: the item, article, file or website for 53 companies. The
  infobox comes first because Wikidata's logo was sometimes outdated or another unit's
  (Kroger 1961-2019, Prudential's Brazilian unit, Discovery for WBD); non-free logos are only
  on Wikipedia. A logo added by hand gets `source: manual` and is not replaced by `--all`.
- Result (2026-10-26): 252 of 256 active companies (223 from Wikipedia, 5 Wikidata, 23
  favicons, 1 by hand: SSRM, whose infobox still shows the Silver Standard logo); none for LRN,
  UWMC, DNLI, ANNX (to add by hand). 2.1 MB of files; the data URIs add about 2.8 MB to the
  app database. Logos were reviewed on the sheet, not one by one: corrections go into
  `config/logos.yaml` or a replaced file.
