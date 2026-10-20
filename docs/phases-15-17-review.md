# Phases 15–17: D&O risk layer, S&P 100, path to ~98% of listed companies

*Review draft, 2026-10-18 (updated after the first full Phase 16 run). Branch `phase16`. Chinese
version: `phases-15-17-review.zh.md`.*

| Phase | Goal | Status |
|---|---|---|
| 15 | Add the main drivers of D&O claims: market moves, insider trades, reporting quality, litigation | Done (decisions D-050, D-051) |
| 16 | Scale from 9 companies to the S&P 100 (107 companies) | First full run done (XBRL and LLM); remaining: notes for 55 text-metric gaps, holdout checks, insight audit, stability check (D-052) |
| 17 | Industry groups for ~98% of US-listed operating companies (~4,100) | Plan; development sample decided (226 companies), then freeze and go live for all |

**What we'd like from you.** Challenge the decisions, thresholds and assumptions, not the
wording. Section 7 lists the questions we most want answered; section 6 lists the risks we
already see. Please reference the section number (e.g. "§4.5") in each comment, or add rows to
the table in §8.

Sources of record: `docs/PLAN.md` (Phases 15, 16, 17, 10), `docs/decisions.md` (D-049–D-052).
New since the first draft: §4.3 (restatements, BlackRock, release checks, parallel insights),
§4.4–4.6 (final numbers), §4.8, §5.7–5.9, risks 9–11, questions 9–11.

---

## 1. Context in one paragraph

The pipeline reads SEC EDGAR filings (10-K, 10-Q, 8-K, proxies, Form 4, 13D) and Alpha Vantage
market data, news and call transcripts; computes metrics per industry; raises risk signals
with fixed rules; has an LLM extract figures from text and write insights; and publishes all of
it to the Professional 360 application database. Rules, mappings and thresholds live in
`config/`, not code. Every value links to its source; LLM output is kept only if its quote and
numbers are found in the source document.

## 2. Summary of results

| | Phase 15 (9 companies) | Phase 16 (107 companies) |
|---|---|---|
| New data | Prices, earnings vs estimates, Form 4, 8-K items, 13D, litigation, auditor fees | 98 companies, 12 new industry groups, ~30 new module metrics |
| New signals | Stock drop, earnings-miss drop, insider cluster selling, near-term refinancing, Altman distress, litigation, cyber (8-K 1.05), activist 13D, auditor change, dual class, non-audit fees | None (same rules, more companies) |
| Evidence | Ford 2024-07-25 drop and miss detected; Form 4 count = EDGAR (0 missing); 41 holdout legal matters typed correctly | 0 unexplained XBRL gaps (from 1,351 at first run); DQ errors 37 → 6; 24 golden tests; 406 tests pass; first LLM pass $13.32 |
| Open | Within-company parallel LLM calls; SecureGPT RPM and price; cold-start timing | Notes for 55 text-metric gaps, text-metric holdout checks, insight audit, stability check |

---

## 3. Phase 15: D&O risk core layer (done)

**Goal.** Cover the main drivers of securities class actions before scaling out: share-price
drops, earnings misses, insider selling, disclosure and accounting problems, litigation and
regulatory investigations. Built and checked on the 9 pilot companies. No change to the
application database schema (option A): new metrics go to `MatrixData`, new events reach
`StrategicRisk` through signals and insights.

### 3.1 What was built

| Item | What | Key rule or result |
|---|---|---|
| 15a Market | Daily adjusted prices (7 years kept), reported vs estimated EPS, SPY as benchmark | Metrics: market cap, 90-day volatility, 52-week max drawdown, EPS surprise. Market cap = quarter-end close × cover-page shares (≤120 days after period end), else diluted weighted shares |
| | Signal `stock_drop` | One-day return ≤ −10% **and** ≥ 8 points below SPY that day |
| | Signal `earnings_miss_drop` | Fall ≥ 8% within 3 trading days after a miss |
| 15b Form 4 | Officers' and directors' trades, originals only (4/A skipped to avoid double counting) | Metrics: insider net sales, officer sell %. A sale is "planned" (10b5-1) when its footnote says so, else the filing's 10b5-1 box applies |
| | Signal `insider_cluster_selling` | ≥ 3 insiders sell on the open market, outside a plan, within 30 days; one signal per episode; Warning if a drop follows within 90 days |
| 15c Reporting quality | Receivable / inventory days, accruals, goodwill / equity and impairment, SBC / revenue, dilution, debt maturities, Altman Z | Threshold signals: near-term debt > cash + short-term investments + operating cash flow; Altman Z < 1.81 |
| 15d 8-K and 13D | 8-K 1.05 cyber (Warning), 1.02 terminated agreement (Info); 13D kept only when the company is the SUBJECT COMPANY | Without the subject check, JPMorgan's own 13Ds about funds showed up as activists |
| 15e Litigation | New `legal` stage reads the latest 10-K and later 10-Qs; two model reads per passage | Kept only if the quote is in the filing and both reads give the same type. Matters are matched across filings. **A company's first run is its baseline**; matters first seen later go to human review |
| | Litigation signals | Securities class action, Wells notice: Critical. Derivative suit, investigation, enforcement: Warning. Window: began in the last 730 days and still disclosed in the last 400 |
| 15f Governance | Auditor fees, dual-class shares, CEO start year (proxy, two reads); auditor change by PCAOB firm ID | Signals: dual class (Info), non-audit fees ≥ 50% of the auditor's fees (Warning) |
| 15h Tables | RevPAR, occupancy, ADR read by two models | Marriott's 15 values unchanged |
| 15i Throughput | Companies of a stage in parallel threads (server databases only), per-model RPM / TPM limiter, per-task max_tokens | — |
| D-051 SecureGPT | api-version 2025-04-01-preview with strict structured outputs; gpt-6-luna for routine calls, gpt-6-sol for second reads and insights; 500K TPM per deployment | gpt-6-luna price is a provisional third-party list price |

### 3.2 Decisions and the alternatives we rejected

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Insider data source | SEC Form 4 (authoritative, free, keyed by CIK) | Alpha Vantage insider endpoint: secondary, ticker-keyed |
| Stock-drop rule | −10% day and ≥ 8 pts below SPY | Absolute drop only: fires on market-wide selloffs |
| App schema | No change (option A) | New tables in the app: needs a BFF change we don't own |
| Refinancing / Altman | Threshold **signals** (reach insights and the app) | DQ warnings: DQ is for data correctness, not business risk |
| Audit facts storage | Columns on `board_composition` (same document, same two-model check) | New `audit_facts` table |

### 3.3 Acceptance evidence

- Ford 2024-07-25: −18.4% while SPY −0.5%, after EPS 0.47 vs 0.68 estimated: both
  `stock_drop` and `earnings_miss_drop` fired.
- Form 4: every filing EDGAR lists is registered (0 missing across 9 companies). No sale left
  unflagged mentions 10b5-1 anywhere in its filing.
- Litigation holdout (PFE, JPM, LUMN, not used to write the prompt): 41 matters, all types
  correct; quotes are grounded by construction.
- Governance: auditor fees found in 7 of 8 proxies (JPM's are prose and the two reads differ);
  CEO start year 5 of 8.

### 3.4 Not done

- LLM calls within one company are still serial and inside the stage's database transaction.
- SecureGPT requests-per-minute quota unknown; gpt-6-luna price provisional.
- The 10-minute cold-start target for 9 companies is not measured (needs a server database and
  a paid run).

---

## 4. Phase 16: S&P 100 (first full run done)

**Goal.** Every company gets the common core (XBRL core plus the Phase 15 layer) and an
industry module, with no per-company customization.

### 4.1 Companies

- The S&P 100 as of 2026-09-21 (100 companies; GOOGL and GOOG are one CIK) plus the 7 pilot
  companies outside it: **107**. CIKs pinned from SEC's ticker list and checked against each
  company's filing record.
- **22 companies (21%) needed a manual industry**, because their SIC code misleads: e.g. 3M
  (SIC 3841, medical) is industrial; Visa and Mastercard (7389, business services) are
  payments; CVS (5912, drug stores) is managed care; Amazon and Booking are internet. Each has a
  written reason in config.
- **Predecessor registrants.** ExxonMobil (2026) and BlackRock (2024) moved to a new holding
  company CIK; their history, latest 10-K and proxy are under the old CIK. New setting
  `predecessor_ciks`: the facts, documents and signals stages read both.

### 4.2 Industry groups and modules

24 groups (12 new). Companies per group in the current list:

| Group | Cos. | Module metrics (XBRL unless marked *text*) |
|---|---|---|
| Software & Internet | 15 | Gross margin, R&D / revenue, deferred revenue and growth, RPO |
| Semiconductors | 11 | Gross margin, R&D / revenue, RPO |
| Pharma & Biotech | 8 | Gross margin, R&D / revenue; *text:* largest product revenue and its share |
| Banks | 8 | Deposits and growth, unrealized securities losses / equity; *text:* uninsured deposits and share, CRE loans and share (plus the existing bank module) |
| Retail & Restaurants | 7 | Gross margin; *text:* comparable sales growth |
| Industrials; Aerospace & Defense | 6; 5 | Warranty reserve, pension liability, RPO |
| Consumer Staples | 6 | Gross margin; *text:* organic sales growth |
| Medtech; Hardware | 5; 4 | Gross margin, R&D / revenue |
| Brokers & Asset Managers | 4 | Financial core (ROA, ROE, assets / equity, leverage); *text / tables:* AUM, VaR, net flows |
| Telecom | 4 | RPO; *text:* postpaid phone net adds, churn |
| Media | 4 | Core only (no module yet) |
| Utilities | 3 | Regulatory assets |
| Transport | 3 | Operating ratio, pension liability |
| Energy | 3 | *text:* production (barrels of oil equivalent per day) |
| Automotive | 3 | Warranty reserve, pension liability; *text:* vehicle deliveries |
| Payments | 2 | *text:* payments volume |
| Managed Care | 2 | *text:* medical cost ratio, members |
| REIT; Insurance | 1; 1 | REIT core only; insurance: financial core + net investment income |

Non-bank financials get a **financial core** (returns, leverage) instead of liquidity ratios
and EBITDA, which mean little for them. Every new text metric is read twice and stored only
when both reads agree.

### 4.3 Engineering changes (small; most work was configuration)

Phase 16 so far: +2,351 / −217 lines outside test fixtures, of which config +1,378 and code
+413 / −117.

- **Fiscal calendars learned from 10-Qs.** Costco and PepsiCo use 12/12/12/16-week years;
  quarter ends fall up to three weeks before month end. Quarter ends now come from the
  company's own 10-Qs; Alpha Vantage dates these quarters at month end, so earnings match
  within 25 days.
- **New unit `count`** (members, vehicles, barrels per day), and a new extraction prompt
  version, `text_metrics_v3`.
- **Tag coverage.** Bank and broker net revenue, utility operating revenue, interest expense
  alternatives, separate depreciation and amortization, several debt combinations, EBITDA
  without an operating income line (pre-tax income + interest + D&A).
- **Coverage notes per industry group** (and `"*"` for all companies) instead of one note per
  company for gaps a whole industry shares.
- **LLM call deadline** of 10 minutes per call, retries included (development only). The
  long stalls in the first run turned out to be the laptop's idle sleep (the process freezes
  and timers stop); long local runs now go under `caffeinate -i`.
- **Restated years.** A fourth quarter is derived as year minus nine months. When a filing in
  between restated a fiscal year (19 of 107 companies since 2022: spin-offs, discontinued
  operations, presentation changes), that quarter carried the whole restatement, e.g. Bank of
  America Q4 2025 revenue 31,180 against about 28,400 on the restated basis. Such a quarter is
  now N/M, and so is anything computed from it; years keep their latest, restated value.
  Rejected: keeping the wrong values (DQ could not see them), or deriving from the year as
  first reported (stored years would then not match their quarters).
- **BlackRock** tags total revenue as contract revenue; its `us-gaap:Revenues` facts (about
  $11–13B a year) are not the total. Company override (`company_overrides/BLK.yaml`).
- **Release vs XBRL checks.** A release that prints a rounded headline ("net income of $1.8
  billion" for 1,777M) agrees when XBRL rounded the same way gives that figure. The release
  definitions now ask for the exact table figure and name the line ("Net income attributable
  to <company>", not "available to common shareholders", "profit" or adjusted). A check that a
  new reading no longer makes is dropped.
- **Parallel insights.** Insights and metricdetail commit their writes before each model call,
  so no transaction is open while waiting for the model; on SQLite they now run 4 companies at
  once (about 1 company a minute instead of one every 3.5 minutes).

### 4.4 Coverage method and result

Loop, repeated about 8 times: run facts and compute for all 107 → coverage report (target 5
fiscal years + 8 quarters per metric) → for each gap, look up what the company actually tags →
add the tag or formula in config (fixes every company that files that way), or write a note
(per group if the whole industry lacks it) → rerun.

| | First run | After the XBRL loop | After the LLM pass |
|---|---|---|---|
| Unexplained XBRL gaps | 1,351 | **0** | **0** |
| Company × metric rows: OK / explained / partial / none | — | 3,575 / 479 / 0 / 96 | 3,616 / 479 / 28 / 27 |

All 55 remaining gaps are text metrics or ratios built on them, e.g. uninsured deposits for 6
of 8 banks, and comparable sales at Walmart. Each needs a note or a fix; that is the open
coverage item.

### 4.5 Quality gates

| Gate | Status |
|---|---|
| Golden values: 2 companies per new or changed module, each value typed from the 10-K / 10-Q | Done: 24 tests (e.g. Microsoft gross margin 225,465 / 331,839; Costco 16-week Q4 = 303,154 − 207,431) |
| Full test suite, lint, types | 406 passed, 4 skipped; clean |
| DQ without errors | 37 errors after the first pass, 6 now (see below) |
| Text metrics: 2 holdout companies per metric | Open |
| `audit-insights` on 20 companies; stability check on 5 | Open |

DQ errors after the first full pass, and what removed them:

| Check | Compared | Errors | Fixed by | Left |
|---|---|---|---|---|
| Quarters add up to the year | 1,422 company × metric × year sets | 4 | BlackRock override; restated-year rule | 0 |
| Release vs XBRL | about 2,200 values | 33 | rounding-aware comparison (11); tighter definitions (9); restated-year rule and dropping stale checks (7, all Bank of America) | 6 |

The 6 left: Bank of America Q3 2025 revenue and net income (the release uses the restated
basis; XBRL catches up with the Q3 2026 10-Q), and 4 GE Vernova quarters where the model took
net income including minority interests. No stored value is affected: XBRL wins.

### 4.6 Run time and cost (local SQLite)

| Step, 107 companies | Time |
|---|---|
| Company + facts + compute | 10 min |
| Market + documents (26,844 Form 4s) | 84 min |
| News; scores | 66 min; 82 min |
| Text metrics (first pass; rerun with the new definitions) | 159 min; 71 min |
| Governance; legal; signals | 35 min; about 50 min; 23 min |
| Insights: one at a time / 4 at a time | 3.5 min per company / 88 min for 95 companies |
| Metricdetail (4 at a time); publish + DQ | 22 min; 3 min |

**LLM cost of the first full pass: $13.32** (6,054 calls, 0 errors), all on a **small development
model** (development only, cost decision). The same tokens at SecureGPT prices (insights at
gpt-6-sol, the rest at gpt-6-luna) come to about **$116**, roughly $1.1 per company, above the
plan's $70–100; second reads at gpt-6-sol would add some, and the gpt-6-luna price is still
provisional.

### 4.7 Deferred (recorded in D-052)

- Text found only in notes or Item 2, which extraction doesn't read: customer concentration,
  oil and gas reserves.
- Modules with one or two S&P 100 companies and company-specific definitions: REIT FFO and
  occupancy, ARPU, utility storm costs, auto recalls, insurance combined ratio.
- Market cap for multi-class companies (Alphabet, Visa, Berkshire) is understated: needs share
  counts per class.
- Wells Fargo and American Express don't tag total loans with standard tags (company-specific
  concepts needed). The gap has a note.

### 4.8 Not done yet in Phase 16

- Notes (or fixes) for the 55 text-metric gaps (§4.4).
- Text metrics: 2 holdout companies per metric checked against the filings.
- `audit-insights` on 20 companies; stability check on 5; SQL Server test suite.
- The 6 remaining DQ errors are explained; GE Vernova could get one more definition rule.

---

## 5. Phase 17: ~98% of listed operating companies (plan, not confirmed)

### 5.1 Universe ("definition A")

Measured on EDGAR 2026-10-18: operating companies listed on NYSE, Nasdaq or Cboe that filed a
10-K after 2024-07-01, excluding ETFs and commodity / crypto trusts: **4,195 companies**.
Out of scope: foreign issuers (20-F / 40-F, 1,124), closed-end funds (355), 286 without a
recent annual report (mostly SPACs).

### 5.2 Coverage today and target

| | Companies | Share |
|---|---|---|
| Phase 16 groups, by SIC | 2,995 | 71.4% |
| + 7 new groups with modules, + 6 mapping additions | 4,003 | 95.4% |
| + 7 mapping-only groups | **4,117** | **98.1%** |

New groups with modules (XBRL first, at most 3 text metrics each): SPAC shells (174), chemicals
and materials (134), mining and metals (89), healthcare services (73), construction and
homebuilders (52), leisure and gaming (38), restaurants (37). Mapping-only groups (core metrics,
no new ones): business services, wholesale, instruments, consumer durables and apparel, real
estate operating, agriculture, consumer services. Nine existing groups get wider SIC ranges.

### 5.3 Clinical-stage trait

Life-science companies with under $10M revenue (390, 9% of the universe) sit in Pharma or
Medtech by SIC, but R&D / revenue means nothing for them. A trait set in company config (the
universe script suggests it, a person approves, reviewed quarterly) swaps in cash runway
(quarters of cash at the recent burn) and R&D share of operating expenses; signal when runway
< 4 quarters.

### 5.4 Prerequisites and scale

- **Phase 10 must come first:** tiered processing (about 800 companies full, 3,200 light,
  insights on demand) and multiple workers claiming tasks. Today one process runs the stages
  in order; companies run in parallel only within a stage (every stage on a server database,
  only insights and metricdetail on SQLite).
- The remaining LLM stages (text metrics, governance, legal, signals, scores, news) move to
  short transactions like insights, and independent calls within a company run together.
- The list (~4,000 CIKs) is generated by a script, reviewed by a person and committed, never
  produced at run time; a quarterly diff report shows listings, delistings, SIC changes.
- Alpha Vantage: ~8,000 calls per day, which needs the premium per-minute quota.
- First-load cost to be extrapolated from Phase 16's measured cost per company, then Tier 1
  sized to the budget.

### 5.5 Remaining ~2% (78 companies) and beyond

48 without a SIC (manual industry each), 17 BDCs and holding companies (need a BDC module), 13
shipping companies (need TCE, fleet utilization). Foreign issuers would be a separate phase
(IFRS, 20-F sections, no Form 4 or proxy).

### 5.6 Decisions Phase 17 needs

1. Are SPACs in underwriting scope? (Without them coverage is still ~98%.)
2. Where the ~4,000-company list lives, and who reviews it quarterly.
3. Clinical-stage revenue threshold ($10M).
4. Tier 1 size and first-load budget.
5. Whether new units (ounces, tonnes, pounds) are display-only or also used in growth and
   thresholds.

### 5.7 Development sample: the 5 largest companies per industry (decided)

Development uses a sample instead of ~4,000 companies: the 5 largest companies by revenue in
each of the 38 groups (Phase 16's 24 plus Phase 17's 14). Measured on EDGAR data: **187
companies** (Payments has only Visa and Mastercard under today's mapping), of which 68 are
already among today's 107, so **119 new** and **226** in all. Scale is about Phase 16's.

The list is reviewed by hand, not taken straight from the ranking:
- Revenue ranking fails in some groups: SPACs have no revenue (use trust size); in brokers,
  StoneX and Galaxy outrank KKR and Blackstone on gross trading revenue.
- SIC misfits need `industry`: Nike (SIC 3021, rubber footwear, lands in chemicals), Cintas
  (apparel SIC; uniform rental), Plains All American, Plains GP and HF Sinclair (SIC 4610
  pipelines, land in transport; midstream energy and refining), Coinbase (lands in credit).
- PayPal, Fiserv and FIS are not in Payments by SIC.

### 5.8 Freeze after the sample, then go live for all companies (decided)

Once the 226 companies pass the quality gates, the solution is no longer tuned per company:
- Changes only at the general level: group SIC mappings, metric sets, tag candidates,
  formulas, group-level coverage notes, DQ rules. Each change applies to the whole industry.
- No company-level settings beyond the sample: no company overrides (like BlackRock's), no
  company coverage notes; `industry` only for misfits found in the sample review or reported
  by underwriters after go-live.
- Beyond the sample, coverage gaps and DQ findings are tracked as per-group rates, not
  explained company by company. A group with a high error rate points to a general rule to fix.
- Prerequisites for going live: Phase 10 (tiers, multiple workers), short transactions in the
  remaining LLM stages, Azure deployment with SecureGPT, Alpha Vantage premium.

### 5.9 Companies outside every industry group: the LLM picks from curated metric sets (decided, PLAN 17g)

When a user asks for a company that maps to no group (e.g. no SIC, BDCs, shipping):
- One LLM call (SecureGPT) reads the business description (10-K Item 1 excerpt, SIC
  description) and the existing groups and metric sets, and picks the closest group or some
  existing metric sets, with reasons quoted from the description.
- Limits: only existing metric sets, never new metrics or formulas; the common core is always
  included; codes are schema-checked; two models must agree, otherwise the second model's
  choice is used and the case goes to the review queue.
- The choice is stored and reused (re-chosen only when the description changes) and shown in
  the app as "metrics chosen by AI from the business description", in the existing
  `Methodology` text (no app schema change).
- Foreign issuers (20-F / 40-F) are out of scope: they need IFRS mapping.

---

## 6. Risks and trade-offs we already see

1. **Same-model dual checks in development.** Text metrics are kept only when two reads
   agree, but in development both reads are the same model (at two effort levels), so agreement is
   weaker evidence than in production (gpt-6-luna vs gpt-6-sol). Holdout checks against the
   filings matter more until a production run exists.
2. **SIC is coarse for large companies.** 21% of the S&P 100 needed a manual industry. Phase
   17 maps ~4,000 companies by SIC ranges with far less manual review; misfiled companies get
   the wrong module silently.
3. **Coverage notes can hide real gaps.** Group-level and `"*"` notes (e.g. "goodwill
   impairment is tagged only in years with one") are efficient, but one broad note explains
   many rows. Every note was checked against company data when written; nothing re-checks it
   later. After the freeze (§5.8), only the sample gets notes; beyond it, gaps are rates.
4. **Litigation baseline.** Matters found on a company's first run are its baseline and do not
   go to review. For the 98 new companies, every existing matter is baseline. They still raise
   signals if recent and active, but nobody reviews them as "new".
5. **Phase 17 assumes a REIT module Phase 16 did not build.** The plan maps "real estate
   operating" companies to "the Phase 16 REIT module (occupancy, NOI)", which was deferred. The
   plan needs that module or a different mapping.
6. **Throughput.** Insights and metricdetail now keep no transaction open while waiting for
   the model: 4 companies at once even on SQLite, measured at 1 company a minute instead of one
   every 3.5 minutes. The other LLM stages still make their calls inside one transaction per
   company: serial on SQLite, holding locks on Azure SQL. Converting them is planned in Phase
   17 (17d), which also depends on Phase 10 (not started).
7. **External dependencies.** Alpha Vantage premium is required (prices, estimates, news);
   SecureGPT RPM quota and the gpt-6-luna price are unknown; Alpha Vantage earnings dates are
   matched to quarter ends within 25 days, looser than an exact date match.
8. **Learned calendars trust the filer.** Quarter ends come from the 10-Q's own fiscal-period
   tags; a mis-tagged 10-Q would shift a quarter. Golden tests cover Costco only.
9. **N/M quarters after restatements.** Restated years leave their fourth quarter (and what is
   computed from it) as N/M in the app until the nine-month figure is restated too, which for
   older years never happens. Correct, but underwriters will see "N/M" for some quarters.
10. **Freeze without per-company tuning (§5.8).** Beyond the sample, SIC misfits keep the wrong
    module and company-specific tagging (like BlackRock's) leaves individual metrics missing
    or off; DQ flags them, nobody fixes them one by one.
11. **AI-chosen metric sets (§5.9)** are the one place where metric lists are not curated by
    people. The limits (existing sets only, two models, stored and reviewable) keep it
    contained, but the choice can still be wrong for an unusual business.

## 7. Questions for the reviewer

1. Are the signal thresholds right for underwriting: stock drop −10% and 8 pts below the
   market; earnings miss −8% within 3 days; insider cluster 3 people in 30 days?
2. Should a company's first-run litigation matters go to review (§6.4), at least for
   securities class actions and investigations?
3. Is SIC plus spot overrides acceptable for Phase 17, or should every company's group be
   reviewed by a person once?
4. Are any coverage notes in `config/coverage_notes.yaml` explaining away a gap that should be
   fixed instead? Spot-check a few groups.
5. Which deferred items (§4.7) matter most for underwriting: customer concentration, reserves,
   REIT occupancy, ARPU, multi-class market cap?
6. Is a same-model dual check acceptable for development, or should text metrics be validated
   on SecureGPT before Phase 16 is called done?
7. For Phase 17: SPACs in scope? $10M clinical-stage threshold? Tier 1 size?
8. Anything an underwriter would expect in the S&P 100 view that isn't in §4.2?
9. Is the freeze after the 226-company sample (§5.8) acceptable, given risk 10?
10. Should AI-chosen metric sets (§5.9) be reviewed by a person before they reach the app, or
    only flagged?
11. For restated quarters (§4.3, risk 9): N/M, or the value as first reported with a label?

## 8. Reviewer notes

| # | Section | Comment | Severity (blocker / should / nice) |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |

---

## Appendix: terms

| Term | Meaning |
|---|---|
| XBRL | Machine-readable tags in SEC filings; the source of financial-statement metrics |
| 10-K / 10-Q / 8-K | Annual report / quarterly report / current report on material events |
| Form 4 | Insider trade report by officers, directors and 10% holders |
| 13D | Filing by an investor who crossed 5% ownership with possible intent to influence |
| 10b5-1 plan | Pre-scheduled trading plan; such sales are excluded from insider-selling signals |
| MD&A | Management's discussion and analysis section, where most text metrics are found |
| EX-99.1 | The earnings press release attached to an 8-K |
| SIC | Standard Industrial Classification code SEC assigns each registrant |
| Golden test | Automated test comparing pipeline output with numbers typed by hand from the filing |
| Coverage note | Written reason why a metric is missing for a company or industry |
| Dual check | Two independent reads of the same text; the value is kept only if they agree |
| Grounding | An LLM answer is kept only if its quote and numbers appear in the source document |
