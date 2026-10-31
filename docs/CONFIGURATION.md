# Configuration guide: what you can change in `config/`

What the pipeline tracks for each company is set by YAML files in `config/`: which industry a
company belongs to, which metrics and risk signals that industry tracks, and how each metric is
calculated or read from filings. You change them by editing the files and opening a pull
request; no code changes are needed for anything in section 2. Section 3 lists what does need
code. Decisions D-016 and D-049 explain the design.

## 1. How to make a change

1. Edit the file (section 2 says which).
2. Validate: `uw-pipeline config check`. It loads `config/` exactly as a run does and lists the
   company and industry settings. A mistake (an unknown metric, industry or signal id, a
   misspelled field, a wrong value type) is reported with what is wrong; a run with that
   mistake would refuse to start.
3. Look at one company: `uw-pipeline config show --ticker MAR`. It prints the company's industry
   and why, the metrics and signals it tracks, and what its industry excludes. Signal ids for
   `exclude_signals` and `signal_params` come from this list.
4. Run the tests (`pytest -q`) and open a pull request.
5. The change takes effect on the next run:
   - Metrics an industry no longer tracks are deleted from the pipeline and app databases, and
     signals no longer tracked are deleted. Moving a company to another industry does both.
   - New formula metrics cost nothing extra. New text metrics (`kind: text`) and new signals
     are read from filings by the LLM for the companies concerned, which costs LLM calls.
   - A new metric that a company should have but has no value for needs a note in
     `coverage_notes.yaml` (`uw-pipeline coverage-report` lists the gaps).

## 2. What you can change

| I want to | File | What to write |
|---|---|---|
| Add a company | `companies.yaml` | `ticker`, `cik` (pinned; the ticker is only for display), `display_name`, `news_aliases`; candidates from `scripts/build_universe.py --sample N` (D-055) |
| Put a company in an industry, whatever its SIC code | `companies.yaml` | `industry: GROUP` and `industry_reason: "..."` (required) |
| Mark a company clinical-stage (pre-revenue life sciences) | `companies.yaml` | `traits: [clinical_stage]`: adds cash runway, R&D / operating expenses and the `clinical` stage; drops the product metrics (`trait_exclude_metrics`); candidates from `scripts/build_universe.py --clinical N` (D-058) |
| Correct a group in the company universe or sample (before the company is added) | `universe.yaml` | under `industry`: `cik`, `ticker`, `industry`, `reason`; `sample_skip` to leave one out of the sample (D-055) |
| Keep a company's history across a new parent registrant | `companies.yaml` | `predecessor_ciks: [old CIK]` (D-052) |
| Create an industry, or map SIC codes to one | `industry_groups.yaml` | a group under `groups` (`label`, `is_financial`, `metric_sets`); codes under `sic` |
| Choose an industry's metrics | `industry_groups.yaml` | `metric_sets`, `metrics` (add codes), `exclude_metrics` (drop codes), `traits` |
| Define a new metric | `metrics.yaml` | an entry under `metrics` (2.3), then add it to a set or an industry |
| Use a new XBRL tag | `concept_map.yaml` | a concept with candidate `tags` (optionally with dimensions) |
| Use a tag only one company files | `company_overrides/<TICKER>.yaml` | `concepts`, `metrics`, `sections` |
| Stop tracking a signal for an industry | `industry_groups.yaml` | `exclude_signals: [rule ids]` |
| Change a signal's threshold for one industry | `industry_groups.yaml` | `signal_params: {rule_id: {field: value}}` |
| Add a keyword signal | `signal_rules.yaml` | an entry under `keywords` (2.4); `industries` to limit it |
| Add a metric threshold signal | `signal_rules.yaml` | an entry under `thresholds` (2.4) |
| Hide or show a whole metric set (computed, but not in the app, the LLM inputs or threshold signals) | `metrics.yaml` | `hidden_metric_sets: [SET, ...]`; remove a set to show it again (D-054) |
| Change an 8-K item's severity or title | `signal_rules.yaml` | `eight_k_items` |
| Change a market, insider, 13D or litigation signal (threshold, window, severity) | `signal_rules.yaml` | `market`, `insiders`, `ownership_forms`, `legal` (2.4) |
| Explain a missing metric | `coverage_notes.yaml` | a note per company and metric (or `"*"`), per industry under `groups:`, or per company trait under `traits:` (2.6) |

### 2.1 A company's industry

```yaml
# companies.yaml
  - ticker: LUV
    cik: 92380
    display_name: Southwest Airlines
    industry: HOTELS                 # a group in industry_groups.yaml
    industry_reason: "Underwrite as a travel company, not an airline"
```

Without `industry`, the company is classified by its SIC code: exact `sic` mapping, then
`sic_ranges`, then `default_group`. SIC ranges must not overlap (the file is rejected on load,
D-055); to carve a code out of a range, map it under `sic` or split the range. `is_financial` on the group decides whether the bank modules
(NIM, CET1, loan book, Performance Trends) get data.

### 2.2 What an industry tracks

```yaml
# industry_groups.yaml
groups:
  RETAIL:
    label: Retail
    is_financial: false
    metric_sets: [COMMERCIAL_CORE]       # named sets in metrics.yaml
    metrics: [comparable_sales]          # extra metric codes
    exclude_metrics: [buyback_intensity] # drop codes that come with a set
    exclude_signals: ["8k_1.01"]         # drop signals; "8k_5.02" also drops its CEO/CFO variants
    signal_params:
      pay_vs_performance: {min_pay_growth: 0.5}
```

- Every group also gets `common_metric_sets` (governance) and the sets of its `traits`.
- An excluded metric that another tracked metric needs (e.g. operating income for EBITDA) is
  still computed, but not shown or used in insights.
- `signal_params` can set any field of that rule: its threshold fields (e.g. `min_pay_growth`,
  `min_ratio`, `value`), `severity` or `title`. A field the rule doesn't have is an error.

### 2.3 A new metric

```yaml
# metrics.yaml, under `metrics:` (after the metrics it uses)
  - code: debt_to_revenue            # stable id; renaming it starts a new metric
    display_name: Total Debt / Revenue
    section: leverage                # financial | liquidity | leverage | cash_flow |
                                     # operational | fi_performance | loan_portfolio
    unit: ratio                      # USD | ratio (1.2 = 1.2x) | fraction (0.05 = 5%) |
                                     # days | shares | score | count
    formula: total_debt / revenue    # concepts or earlier metric codes; + - * / ( )
    nm_when: revenue <= 0            # optional: shown as N/M when true
```

| Kind | Fields | Example |
|---|---|---|
| `formula` (default) | `formula`: one expression, or a list of alternatives (the first that evaluates wins); `optional`: names counted as 0 when missing; `x_py` = x a year earlier | `net_income / ((total_assets + total_assets_py) / 2)` |
| `growth` | `base` (metric code), `compare`: `prior_quarter` or `prior_year` | revenue growth |
| `text` | `extraction`: `definition` (exact scope, what to exclude), `keywords`; optional `dual_check: true` (two models must agree), `table` (read by cell from a table) | RevPAR, NIM |
| `external` | none: written by another stage (market data, Form 4); listing it in a set shows it | market_cap, insider_net_sales |

Other fields: `description`, `period_types: [Q, FY]`, `display: false` (computed but hidden),
`insight_input: true` (hidden but used in insights). The header of `metrics.yaml` describes
every field; existing metrics there are the best examples. A text metric's `definition` should
be precise: the LLM reads it, and every value must be quoted from the filing and pass the
period and unit checks.

### 2.4 A new signal

```yaml
# signal_rules.yaml
keywords:
  - id: shrink_language
    category: Risk & Legal
    subcategory: Inventory Shrink
    severity: Warning                # Info | Warning | Critical
    title: Shrink or organized retail crime mentioned
    sections: ["10K-1A", "10Q-II-1A"]  # section-code prefixes
    pattern: '\b(shrink|organized retail crime)\b'
    exclude: '\bno\b'                # optional: skip a match whose sentence matches this
    industries: [RETAIL]             # optional: only these groups

thresholds:
  - id: thin_liquidity
    category: Risk & Legal
    subcategory: Liquidity & Leverage
    severity: Warning
    title: Current ratio below 1.0
    metric: current_ratio
    op: "<"                          # < <= > >=
    value: 1.0                       # stored units: fraction 0.05 = 5%, ratio, USD
    period: Q                        # latest quarter (Q) or latest fiscal year (FY)
    industries: [RETAIL]             # optional
```

Other signal sections in `signal_rules.yaml` (D-050) take the same `category`, `subcategory`,
`severity` and `title`, plus their own settings, and each can be set per industry with
`signal_params` under its rule id:

| Section | Rule ids | Settings |
|---|---|---|
| `market` | `stock_drop`, `earnings_miss_drop` | `min_drop`, `min_excess` (vs `benchmark`); `days` after a miss |
| `insiders` | `insider_cluster_selling` | `min_insiders`, `window_days`, `escalate_days`, `lookback_days` |
| `ownership_forms` | `13d`, `13d_amendment` | per form (`SCHEDULE 13D`, `SC 13D/A`, ...) |
| `legal` | `legal_securities_class_action`, `legal_derivative`, `legal_regulatory_investigation`, `legal_enforcement`, `legal_wells_notice` | `lookback_days`, `active_days` (whole section) |
| `metrics` | `dual_class_shares`, `non_audit_fees`, board rules | `min_share`, `min_ratio` |

Section codes: `10K-1A` risk factors, `10K-3` legal proceedings, `10K-7` MD&A, `10K-9A`
controls, `10K-AUDIT` auditor's report, `10Q-I-2` MD&A, `10Q-I-4` controls, `10Q-II-1` legal,
`10Q-II-1A` risk factor update, `8K-<item>` 8-K items. `pattern` is a regular expression
searched in the section text, case-insensitive; `exclude` is checked against the sentence
around a match. Test a new pattern on a real filing first.

### 2.5 Company history across registrants

A holding-company reorganization gives a company a new CIK (ExxonMobil 2026, BlackRock 2024):
the ticker moves to the new registrant, while the history, the latest 10-K and proxy may still
be the old registrant's. `cik` is the new one; `predecessor_ciks` lists the old ones, and the
facts, documents and signals stages read their filings too. A company with `history_start`
(successor-only history after a merger, D-012) reads its own filings only.

### 2.6 Coverage notes

`coverage-report` flags every metric short of 5 fiscal years and 8 quarters without a note.
A company whose own 10-K / 10-Q filings start inside that window (a listing, a spin-off, a
move from 20-F) is held to what they can hold: fiscal years from the earliest balance sheet
in its first filing, quarters from its first 10-Q (D-063).

```yaml
# coverage_notes.yaml
groups:
  "*":                       # every company
    goodwill_impairment: Tagged only in years with an impairment.
  SOFTWARE_INTERNET:         # every company of the group
    inventory_days: Software companies hold little or no inventory.
traits:
  clinical_stage:            # every company with this trait; wins over its group's note
    revenue: Pre-revenue.
MSFT:                        # one company; wins over its traits' and group's notes
  "*": Explains every gap of this company.
  gross_margin: ...
```

### 2.7 Other settings

| File | Controls |
|---|---|
| `insights.yaml` | Filing sections and sizes in the insight input, transcripts and news days, the news materiality gate, insight counts |
| `news.yaml`, `scores.yaml` | News de-duplication and relevance; news and transcript risk scoring |
| `text_extraction.yaml` | Text-metric chunk budget, extraction effort, loan portfolio |
| `dq_rules.yaml` | Data-quality thresholds and metric identities |
| `app_publish.yaml` | Metric categories, risk type rules and confidence in the app database |
| `source_labels.yaml` | Readable names of sections and concepts |
| `jobs.yaml` | Jobs and the stages each runs; retries, run lock and resume settings |

## 3. What needs a code change

| Change | Why |
|---|---|
| A new judgment question (decided by rules plus a model vote, like going concern or a CEO change) | Its answer types and output format are in code and the prompt. Existing ones can be excluded per industry with `exclude_signals`. |
| A new metric or board signal type beyond thresholds (two metrics, trends, several periods) | Each type is a function in `signals/detect.py`. |
| Different insight tasks per industry, or a new kind of insight | Behavioral, strategic & risk, key insights and follow-up questions run for every company; their wording is in versioned prompts. |
| Changing a prompt | Prompts live in `src/uw_pipeline/enrich/prompts/`; a change needs a new version and a review of the golden outputs. |
| Functions in formulas (min, max, abs, if) | Formulas support + - * /, unary minus, parentheses; comparisons only in `nm_when`. |
| Units other than USD, ratio, fraction, days, shares, score, count, ounces, pounds, tons, months; periods other than quarter and fiscal year | Fixed in the metric model and the period logic. |
| A new market, insider or litigation metric (`kind: external`) | Written by the market or documents stage in code. |
| A new section code or document type | Section splitting is in code. |
| New data sources, a new LLM provider | Clients live in `src/uw_pipeline/clients/`. |
| App database tables or columns | Schema changes go through `schema_app.py` and the reviewed DDL in `deploy/sql/`. |

## 4. Rules to keep

- A company is identified by its pinned CIK, never by its ticker.
- Metric lists are curated here, never generated at run time.
- Keep metric codes and signal ids stable: a new code is a new metric, and old values are
  removed.
- Every metric gap that `coverage-report` shows needs a note in `coverage_notes.yaml`.
- Golden test values come from the filing itself.
- No secrets in `config/`: credentials come from environment variables only.
