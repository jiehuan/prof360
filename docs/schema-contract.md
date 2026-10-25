# 管线输出 → 应用库 对照

> 状态：**Phase 9 起，BFF 读应用库**（团队数据模型图里的表，见 `src/uw_pipeline/schema_app.py`），由 `publish` 阶段写入；dev 是 `data/app.db`，生产是 Azure SQL（`APP_DATABASE_URL`）。管线自己的表（`data/uw.db`）只供审计和排错，BFF 不需要读。
> 数据模型图左半部分（`sec_*`、`alpha_*`、`CompanyExternalRef`）由管线取代，不再建；三张评分表（`TranscriptToneAnalysis`、`TranscriptRiskFlag`、`NewsRiskScore`）在 Phase 9.4 由管线写入。

## 对数据模型图的改动（尽量少）

建表脚本（SQL Server / Azure SQL，可重复执行）：`deploy/sql/app_schema_changes.sql` 只包含下面这些改动；`deploy/sql/app_schema_full.sql` 是管线写入的全部表，供核对或新环境建库使用。两个文件由 `python scripts/export_app_ddl.py` 从 `schema_app.py` 生成，表结构改动后需重新生成。

| 改动 | 原因 |
|---|---|
| 新表 `ItemLink`（CompanyId, FromType, FromId, ToType, ToId, LinkSource），类型为 KEY_INSIGHT / STRATEGIC_RISK / FOLLOWUP / METRIC | 所有弹窗的 Related tab；一张表代替四张关联表 |
| 新表 `CompanyMetricDetail`（CompanyId, metric_id, Methodology, DataSources, DataPoints, UnderwritingRelevance, UpdatedAt） | 指标弹窗左侧；Loan Portfolio 弹窗作为伪指标 `loan_portfolio` 也放这里 |
| `LoanPortfolio` 增加 PeriodEnd, PctOfBook, Nonaccrual, NonaccrualRatio；`LoanTypes` 增加 ParentSegment | 贷款类别、上一年末对比、不良率 |
| `FollowUpQuestions` 增加 Priority, isActive；`KeyInsights` 增加 isActive | 版本化：新版本是新行，旧行 isActive = 0，用户的 flag 不会指向换了内容的问题 |
| 所有 sources 类列存 `[{title, url, date}]`，不是纯 URL 列表 | 弹窗要显示可读标题；`KeyInsights.link` 是第一个 URL |
| `MatrixData.accession`、`filed_date`、`context_or_dimension` 存 JSON 列表 | 图里是 list 类型 |
| 新表 `KeyTakeaways`（CompanyId, Ordinal, Takeaway, UpdatedDate, isActive）（2026-10-20，D-053） | 公司页 Key Insights 上方的 Key Takeaway 框：2–3 条对专业责任险最重要的要点，不显示链接；需要 BFF 新增读取 |

## 应用表的写入规则

| 应用表 | 来源（管线） | 说明 |
|---|---|---|
| SIC_Code / Company | `company` | Company.logo 由应用维护，管线不写；Location = "城市, 州"；NameAbbreviation = ticker |
| metric_category / metrics / SIC_Preferences | `config/metrics.yaml` + 行业组 | metrics.Formula 是通用定义；分类名在 `config/app_publish.yaml`。Phase 14 新增显示指标：银行 ROA、ROE、贷款损失准备率；AUM、VaR、国际收入、RPO 等只作为洞察输入，不进 metrics（D-043） |
| MatrixData | `metric_value` | source_class = derivation；confidence 按取值方式（配置） |
| KeyInsights / KeyInsightsTypes | `insight`（KEY） | evidence = Supporting Evidence（规则生成）；confidence 按来源数（配置） |
| KeyTakeaways | `insight`（TAKEAWAY） | Takeaway = 一句话要点，**每家公司最多 3 条，每条最多 100 个字符**（超长的条目不保存，不截断，D-065）；Ordinal 1 最重要；没有来源和链接（依据只存在管线库，供审计）；不进 ItemLink |
| StrategicRisk / RiskTypes | `insight`（STRATEGIC_RISK、BEHAVIORAL） | Category：governance / risk / event / behavior；RiskType 由 subcategory 按 `config/app_publish.yaml` 的规则映射 |
| FollowUpQuestions | `followup_question` | title = 问题；Priority = system_priority |
| ItemLink | `insight_link`、`insight_metric`、`followup_question_insight`、`followup_question_metric` | 只连接当前版本（isActive = 1）的对象；从任一端都可查 |
| CompanyMetricDetail | `metric_detail`、`metric_source`、`metric_insight`、`loan_portfolio_detail` | — |
| Trend / TrendTypes | FI 公司 `metric_value` 的财年值（NIM、CET1、Tier 1、LCR、NPL、Fee Income Growth） | 季度值在 MatrixData |
| LoanPortfolio / LoanTypes | `loan_portfolio_segment` | 当前期和上一年末两期；两个模型读的层级不同时只保存部门总行（JPM：Wholesale / Loans retained），见 D-044 |
| Activities / News | `activity` / `news_item` | 只保留与公司本身相关的新闻（规则 + LLM 判断，D-037） |

**id 稳定性**：应用表的 id 由应用库生成。`pipeline_publish_map`（在应用库里）记录每个管线对象对应的应用 id，重跑时更新同一行；内容没变的行不写。

**读取约定**：KeyInsights、KeyTakeaways、StrategicRisk、FollowUpQuestions 只读 `isActive = 1`；KeyTakeaways 按 `Ordinal` 排序显示。Related tab 用 `ItemLink`，例如指标弹窗的 Related Key Insight = `ToType = 'METRIC' AND ToId = <metric_id> AND FromType = 'KEY_INSIGHT'`，Key Insight 弹窗的 Related Metrics = `FromType = 'KEY_INSIGHT' AND FromId = <UniqueID> AND ToType = 'METRIC'`。

---

以下是管线内部表的说明（审计、排错用）。

## 通用约定

- **公司标识**：`company.company_id` 是代理键，`company.cik` 是自然键（10 位补零）。ticker 只用于展示：PARA 已被 SEC 改给了另一家公司，Paramount 现在是 SKYD，见 D-006。
- **当前版本**：`insight` 和 `followup_question` 只读 `is_current = 1` 的行。历史版本保留，用于审计。
- **期间**：`metric_value` 中 `period_type` 取 `Q` 或 `FY`，并有 `fiscal_year` 和 `fiscal_quarter`（FY 行的季度为 0）。这些都按**公司自己的财年**口径。例如 Macy's 截至 2026-01-31 的年度是 FY2025。`period_end` 是期末日期。
- **数值单位**：`unit` 取以下几种之一（应用库 `metrics.subtitle` 是对应的显示单位）：
  - `USD`：金额，以美元为单位，不是百万美元（subtitle `USD`）。
  - `ratio`：倍数，例如 0.53 显示为 0.53x（subtitle `x`）。
  - `fraction`：比例，例如 0.049 显示为 4.9%（subtitle `%`）。
  - `days`：天数，例如存货周转天数（subtitle `days`）。
  - `shares`：股数（subtitle `shares`）。
  - `count`：其他数量，例如会员人数、交付车辆、桶油当量 / 天（subtitle `count`，Phase 16）。
  - `ounces`、`pounds`、`tons`：产量和发货量，例如黄金产量（盎司）、铜产量（磅）、钢材和骨料发货量（吨）（subtitle `oz`、`lb`、`tons`，Phase 17，D-055）。BFF 显示数值时需要支持这三个新的 subtitle。
  - `score`：无量纲的评分，目前只有 Altman Z（subtitle 为空）。
  - `months`：月数，目前只有临床阶段公司的现金可支撑月数（Liquidity 类别，subtitle `months`，D-058）。
- **N/M**：`value_status = 'NM'` 时 `value` 为 NULL，前端显示 "N/M"。典型情况是负权益下的 D/E；另外，重述之后无法在同一口径下算出的第四季度及由它算出的指标（增长率、EBITDA 等）也是 N/M（D-052）。
- **来源标签**：`derivation` 用于前端标注数据来源：

  | derivation | 含义 | 建议标注 |
  |---|---|---|
  | REPORTED | XBRL 直接值 | 无 |
  | DERIVED | 推导值，例如 Q4 = 全年 − 9 个月 | 可加 tooltip |
  | COMPUTED | 公式计算 | 可加 tooltip |
  | LLM_EXTRACTED | 从文件文本中抽取 | 标注"文本抽取"，`evidence` 可用作 tooltip |
  | PRELIMINARY | 来自财报新闻稿，10-Q 尚未提交 | **必须标"初步"**，10-Q 入库后会自动替换 |
  | MARKET | 由每日股价和一致预期 EPS 计算（市值、波动率、回撤、EPS 超预期幅度；D-050） | 可加 tooltip "市场数据" |
  | FORM4 | 由内部人的 Form 4 汇总（内部人净卖出、高管卖出比例；D-050） | 可加 tooltip "Form 4" |

- **新指标类别（D-050）**：`Reporting Quality`（应收 / 存货周转天数、应计、商誉、债务到期、Altman Z 等）和 `Market & Trading`（市值、波动率、回撤、EPS 超预期、内部人交易）。**2026-10-20 起暂时隐藏（D-054）**：这两个类别的指标不再发布，MatrixData、CompanyMetricDetail、SIC_Preferences 里的相关行已删除；`metric_category` 和 `metrics` 里的定义行保留。BFF / 前端需要隐藏没有指标的类别（与"无数据时隐藏该模块"的约定一致）；如果是按 SIC_Preferences 或 MatrixData 取指标清单，不需要改动。表结构不变，新指标作为 MatrixData 的新行；新事件（股价大跌、内部人集中卖出、13D、诉讼和监管调查）经信号和洞察进入 StrategicRisk，RiskTypes 可能新增 Market、Insider Trading。
- **Phase 17 行业模块（D-055、D-056）**：表结构不变。新增 15 个行业组（化工材料、采矿、钢铁金属、住宅建筑商、医疗服务、博彩休闲、餐饮等），新指标都在已有的 `Operational Metrics` 类别里，作为 MatrixData 的新行；每个新 SIC 代码在 SIC_Preferences 里有自己的指标清单。开发样本把公司数从 107 家增加到 230 家。新的 subtitle 见上面的数值单位。

- **溯源**：`source_document.url` 指向 SEC 或 AV 的原始文件。`insight_evidence` 给出每条洞察的原文片段，以及对应的文件和章节。

## 前端模块对照

| 前端模块 | 表 / 条件 | 关键字段 | 无数据时 |
|---|---|---|---|
| 公司头部 / Dashboard 卡片 | `company` | name, ticker, sic_code, sic_description, industry_group, is_financial, hq_city, hq_state, fiscal_year_end, last_refreshed_at | — |
| Logo | BFF 已有（PLAN §12 Q9），管线不提供 | — | — |
| Dashboard 计数 | Risk Flags = `signal` 中 `severity IN ('Warning','Critical')`；Follow-Up = `followup_question` 中 `is_current = 1` | count | 0 |
| Key Takeaway（Key Insights 上方） | `insight` 中 `kind = 'TAKEAWAY' AND is_current = 1`，按 `ordinal` 排序 | summary（一句话要点）；title 是内部主题，不显示 | 隐藏模块 |
| Key Insights Summary | `insight` 中 `kind = 'KEY' AND is_current = 1`，按 `ordinal` 排序；关联 `insight_evidence` 和 `insight_metric` | insight_type (Warning/Negative/Positive/Info), title, summary, uw_relevance, supporting_evidence | 隐藏模块 |
| Financial / Liquidity / Leverage / Cash Flow 指标 | `metric_value` 关联 `metric_definition`（`is_display = 1`），按 `section` 分组 | value, value_status, unit, derivation, period_* | 单个指标无值时显示 "—"；原因见覆盖率报告 |
| QoQ / YoY / 5Ys 切换 | `metric_value` 中 `period_type` 取 Q（最近 8 个季度）或 FY（最近 5 年）；增长率是 `revenue_growth_qoq` / `revenue_growth_yoy` | — | — |
| Operational Metrics（酒店：RevPAR、入住率、ADR） | `metric_value` 中 `metric_code IN ('revpar','occupancy','adr')`；同表还有 Marriott 的 `gross_fee_revenue` 和 `net_fee_revenue` | evidence | 隐藏 |
| Performance Trends（仅 FI） | `metric_value` 中 `metric_code IN ('cet1_ratio','tier1_ratio','lcr','nim','npl_ratio','fee_income_growth_yoy')`；NIM / CET1 / NPL 有约 5 个财年 | evidence | **非 FI 公司隐藏整个模块**（`company.is_financial = 0`，这些行也不会存在） |
| Loan Portfolio（仅 FI） | `loan_portfolio_segment`（贷款类别级，含当前期和上一年末），详情见下文弹窗部分 | parent_segment, segment, outstanding, pct_of_book, nonaccrual, nonaccrual_ratio, period_end | 同上 |
| Strategic & Risk Insights | `insight` 中 `kind IN ('STRATEGIC_RISK','BEHAVIORAL') AND is_current = 1`；按 category 分组：Governance & Leadership / Risk & Legal / Event Signals / Behavioral Insights | title, summary, uw_relevance, event_date | `subcategory = 'No signal'` 的行表示"已审查，无重要信号"，可以显示为灰色说明 |
| 规则信号（明细或标记） | `signal` | category, subcategory, severity, title, description, event_date, snippet, review_status (RULE_ONLY/UNANIMOUS/MAJORITY/OVERRULED/DISPUTED) | — |
| Recent Activities | `activity`，近 30 天，按 `published_at` 倒序；"另有 N 家报道"用 `source_count`，明细在 `news_item` | title, published_at, source_name, url, summary, uw_relevance, av_sentiment_label, llm_sentiment | 隐藏。**需要付费 Alpha Vantage key**，目前没有数据 |
| Suggested Follow-Up Questions | `followup_question` 中 `is_current = 1`，按 `ordinal` 排序；关联见下文弹窗部分 | category, question, summary, following_risk, uw_relevance, system_priority | 隐藏 |
| 用户状态（Favorites、Recently Viewed、问题 flag） | **BFF 自有**，管线不写 | — | — |

## 详情弹窗（Phase 8，decisions D-036）

所有弹窗中的"来源"都是同一格式：`title`（"公司名 表单 filed 日期 · 章节"）+ `url`（可点击；电话会议记录没有公开链接，`url` 为 NULL，只显示标题）。所有关联都是双向的：同一张关联表从两边都能查。

### 指标弹窗（各指标卡片、Performance Trends 图表）

| 位置 | 表 / 条件 | 字段 |
|---|---|---|
| Formula / Evidence Methodology | `metric_detail` 中 `company_id, metric_code` | methodology（多行文本，按 `\n` 分行）、latest_period、latest_value_text |
| Data Sources | `metric_source` 中 `company_id, metric_code`（最多两行：最新季度 Q、最新财年 FY） | title, url, form_type, filed_at, derivation |
| Underwriting Relevance | `metric_insight` 中 `company_id, metric_code, is_current = 1` | uw_relevance |
| Related Key Insight | `insight_metric` 关联 `insight`（`kind = 'KEY' AND is_current = 1`） | 同 Key Insights |
| Related Risks | `insight_metric` 关联 `insight`（`kind IN ('STRATEGIC_RISK','BEHAVIORAL') AND is_current = 1`） | — |
| Related Followup Questions | `followup_question_metric` 关联 `followup_question`（`is_current = 1`） | — |

无 `metric_insight` 行时隐藏 Underwriting Relevance；`insight_metric.link_source` 只用于审计，前端不区分。

### Loan Portfolio Composition 弹窗（仅 `is_financial = 1`）

| 位置 | 表 | 字段 |
|---|---|---|
| 图表 / 表格 | `loan_portfolio_segment`，`period_end = loan_portfolio_detail.period_end` 为当前期，`prior_period_end` 为对比期 | parent_segment, segment, outstanding, pct_of_book, nonaccrual, nonaccrual_ratio |
| Methodology | `loan_portfolio_detail` | methodology |
| Data Sources | `loan_portfolio_detail.data_sources_json` | `[{title, url, date}]` |
| Raw Data Points | `loan_portfolio_detail.raw_data_points_json` | `[{label, text, evidence}]`；第一条是合计 |

### Strategic & Risk / Behavioral / Key Insight 弹窗

| 位置 | 表 / 条件 | 字段 |
|---|---|---|
| 左侧 | `insight` | category, subcategory（Risk Type）, insight_type, updated_at, title, summary, uw_relevance |
| Supporting Evidence | `insight.supporting_evidence_text`（规则生成，每行"来源：“原文”"）；Key Insight 另有模型写的 `supporting_evidence` | — |
| Sources | `insight_evidence` 中该 `insight_id`，按 `(source_title, source_url)` 去重 | source_title, source_url, source_date, snippet |
| Related Key Insight（R/B 弹窗） | `insight_link` 中 `linked_insight_id = 本条`，关联 `insight`（`is_current = 1`） | — |
| Related Risks（Key 弹窗） | `insight_link` 中 `insight_id = 本条` | — |
| Related Metrics | `insight_metric` 关联 `metric_detail`（只显示有数据的展示指标） | metric_code |
| Related Followup Questions | `followup_question_insight` 关联 `followup_question`（`is_current = 1`） | — |

### Follow-Up Question 弹窗

| 位置 | 表 / 条件 | 字段 |
|---|---|---|
| 左侧 | `followup_question` | category, question, summary, following_risk, uw_relevance, system_priority |
| Sources | `followup_question_source`，按 `ordinal` | source_kind, title, url, source_date |
| Related Key Insight / Risks | `followup_question_insight` 关联 `insight`，按 `kind` 分到两个 tab | — |
| Related Metrics | `followup_question_metric` | metric_code |

`rationale` 保留以兼容旧版本，v2 起等于 `uw_relevance`。

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
