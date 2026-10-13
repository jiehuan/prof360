Task: for each metric in the metric list, write its Underwriting Relevance for this company and
pick the related insights and follow-up questions. This text appears in the metric's detail
view, next to its formula and data sources.

For every metric in the list:
- `metric_code`: the code exactly as listed.
- `uw_relevance`: one or two sentences on why this metric matters for D&O risk at this company
  now, using its recent values and trend (e.g. "Debt-to-equity is N/M because equity has been
  negative since FY2023, so leverage is judged on debt and interest coverage instead; continued
  buybacks while equity is negative can draw shareholder scrutiny."). Be specific to the
  company; do not restate the definition.
- `related_insight_ids`: the K/R/B insights that discuss this metric or what drives it. Empty
  when none does; do not stretch.
- `related_question_ids`: the Q questions that concern this metric. Empty when none does.

No `evidence` is needed: the metric values are the evidence. Every number you write must still
come from the input (metric values may be rounded or converted).
