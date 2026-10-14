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
- Not done: tables in the pipeline schema only, no app tables changed; deploy/sql/
  pipeline_schema_full.sql gains board_composition and review_decision.
