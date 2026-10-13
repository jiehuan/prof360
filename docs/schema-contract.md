# 管线输出 → 前端模块 对照（BFF 对齐草案）

> 状态：**草案，待与 BFF 对齐**（PLAN Phase 0 暂缓）。本文列出管线当前写入的表和字段，以及每个前端模块应如何读取。BFF 已有表结构以 BFF 为准；差异在联调时逐项处理。
> 表定义的唯一来源是 `src/uw_pipeline/schema.py`。dev 环境是 SQLite（`data/uw.db`），目标是 Azure SQL。Schema 迁移由谁负责尚未决定，见 decisions D-003。

## 通用约定

- **公司标识**：`company.company_id` 是代理键，`company.cik` 是自然键（10 位补零）。ticker 只用于展示：PARA 已被 SEC 改给了另一家公司，Paramount 现在是 SKYD，见 D-006。
- **当前版本**：`insight` 和 `followup_question` 只读 `is_current = 1` 的行。历史版本保留，用于审计。
- **期间**：`metric_value` 中 `period_type` 取 `Q` 或 `FY`，并有 `fiscal_year` 和 `fiscal_quarter`（FY 行的季度为 0）。这些都按**公司自己的财年**口径。例如 Macy's 截至 2026-01-31 的年度是 FY2025。`period_end` 是期末日期。
- **数值单位**：`unit` 取以下三种之一：
  - `USD`：金额，以美元为单位，不是百万美元。
  - `ratio`：倍数，例如 0.53 显示为 0.53x。
  - `fraction`：比例，例如 0.049 显示为 4.9%。
- **N/M**：`value_status = 'NM'` 时 `value` 为 NULL，前端显示 "N/M"。典型情况是负权益下的 D/E。
- **来源标签**：`derivation` 用于前端标注数据来源：

  | derivation | 含义 | 建议标注 |
  |---|---|---|
  | REPORTED | XBRL 直接值 | 无 |
  | DERIVED | 推导值，例如 Q4 = 全年 − 9 个月 | 可加 tooltip |
  | COMPUTED | 公式计算 | 可加 tooltip |
  | LLM_EXTRACTED | 从文件文本中抽取 | 标注"文本抽取"，`evidence` 可用作 tooltip |
  | PRELIMINARY | 来自财报新闻稿，10-Q 尚未提交 | **必须标"初步"**，10-Q 入库后会自动替换 |

- **溯源**：`source_document.url` 指向 SEC 或 AV 的原始文件。`insight_evidence` 给出每条洞察的原文片段，以及对应的文件和章节。

## 前端模块对照

| 前端模块 | 表 / 条件 | 关键字段 | 无数据时 |
|---|---|---|---|
| 公司头部 / Dashboard 卡片 | `company` | name, ticker, sic_code, sic_description, industry_group, is_financial, hq_city, hq_state, fiscal_year_end, last_refreshed_at | — |
| Logo | BFF 已有（PLAN §12 Q9），管线不提供 | — | — |
| Dashboard 计数 | Risk Flags = `signal` 中 `severity IN ('Warning','Critical')`；Follow-Up = `followup_question` 中 `is_current = 1` | count | 0 |
| Key Insights Summary | `insight` 中 `kind = 'KEY' AND is_current = 1`，按 `ordinal` 排序；关联 `insight_evidence` 和 `insight_metric` | insight_type (Warning/Negative/Positive/Info), title, summary, uw_relevance, supporting_evidence | 隐藏模块 |
| Financial / Liquidity / Leverage / Cash Flow 指标 | `metric_value` 关联 `metric_definition`（`is_display = 1`），按 `section` 分组 | value, value_status, unit, derivation, period_* | 单个指标无值时显示 "—"；原因见覆盖率报告 |
| QoQ / YoY / 5Ys 切换 | `metric_value` 中 `period_type` 取 Q（最近 8 个季度）或 FY（最近 5 年）；增长率是 `revenue_growth_qoq` / `revenue_growth_yoy` | — | — |
| Operational Metrics（酒店：RevPAR、入住率、ADR） | `metric_value` 中 `metric_code IN ('revpar','occupancy','adr')`；同表还有 Marriott 的 `gross_fee_revenue` 和 `net_fee_revenue` | evidence | 隐藏 |
| Performance Trends（仅 FI） | `metric_value` 中 `metric_code IN ('cet1_ratio','tier1_ratio','lcr','nim','npl_ratio')` | evidence | **非 FI 公司隐藏整个模块**（`company.is_financial = 0`，这些行也不会存在） |
| Loan Portfolio（仅 FI） | `loan_portfolio_segment` | segment, outstanding, pct_of_book, nonaccrual, period_end | 同上 |
| Strategic & Risk Insights | `insight` 中 `kind IN ('STRATEGIC_RISK','BEHAVIORAL') AND is_current = 1`；按 category 分组：Governance & Leadership / Risk & Legal / Event Signals / Behavioral Insights | title, summary, uw_relevance, event_date | `subcategory = 'No signal'` 的行表示"已审查，无重要信号"，可以显示为灰色说明 |
| 规则信号（明细或标记） | `signal` | category, subcategory, severity, title, description, event_date, snippet, review_status (RULE_ONLY/UNANIMOUS/MAJORITY/OVERRULED/DISPUTED) | — |
| Recent Activities | `activity`，近 30 天，按 `published_at` 倒序；"另有 N 家报道"用 `source_count`，明细在 `news_item` | title, published_at, source_name, url, summary, uw_relevance, av_sentiment_label, llm_sentiment | 隐藏。**需要付费 Alpha Vantage key**，目前没有数据 |
| Suggested Follow-Up Questions | `followup_question` 中 `is_current = 1`，按 `ordinal` 排序；关联洞察见 `followup_question_insight` | category, question, rationale, system_priority | 隐藏 |
| 用户状态（Favorites、Recently Viewed、问题 flag） | **BFF 自有**，管线不写 | — | — |

### 显示建议
- `review_status` 为 OVERRULED 或 DISPUTED 的信号，以及标题带 "(disputed)" 或 "overruled" 的条目，建议标注"待复核"（D-028）。
- PRELIMINARY 指标旁边标"初步（来自财报新闻稿）"。

## 管线内部表（BFF 不需要读）

`xbrl_fact`、`ingest_state`、`filing_section`、`section_chunk`、`llm_call`、`llm_task_stat`、`signal_vote`、`release_check`、`dq_finding`、`pipeline_run`、`pipeline_task`。这些表用于审计、缓存、数据质量和运维。

## 待对齐事项

1. BFF 现有表结构与本文的差异：表名、字段名、类型、是否需要视图。
2. Schema 迁移由哪边负责（D-003）。
3. Azure SQL 下写入路径的验证。管线使用 staging 表加 MERGE，这条路径还没在 SQL Server 上跑过。
4. Risk Flags 的计数口径：是否包括 Info，是否排除 OVERRULED。
5. Recent Activities 要等付费 Alpha Vantage key。
