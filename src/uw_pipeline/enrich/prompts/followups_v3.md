Task: 10 to 15 follow-up questions an underwriter could ask the company, based on the insights
(K, R and B items) and the metrics.

- `category`: Financial, Operational, Market, Strategy or Governance.
- Make them specific to this company and its industry, referring to the actual metric, event
  or statement (for a hotel company e.g. occupancy, RevPAR, fee revenue, owner relationships;
  for a bank e.g. capital ratios, credit quality, liquidity; for technology e.g. customer churn,
  security incidents).
- `summary`: one or two sentences of background facts from the input that lead to the
  question (what happened or what the numbers show). Facts only, no opinion.
- `following_risk`: the risk area the question probes, at most 5 words (e.g. "Refinancing and
  liquidity risk", "Key-person risk", "Securities disclosure risk").
- `uw_relevance`: one sentence on why the answer matters for D&O risk.
- `system_priority`: high for questions about Warning/Negative items or material uncertainty,
  medium or low otherwise. Use high sparingly (at most a third).
- `related_insight_ids`: the K/R/B ids the question follows from (at least one).
- `related_metric_ids`: the ids of the metric rows (M…) the question concerns, if any. Pick
  the latest period of each metric that matters; may be empty.
- `evidence`: not needed for questions.
- Order: list the questions from most to least important for the underwriter: every high
  question before the medium ones, every medium before the low ones, and within a priority the
  question about the most material risk first. The page shows them in this order.
