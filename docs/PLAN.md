# Underwriting Intelligence — 数据管线开发计划（SEC + Alpha Vantage → Azure SQL）

> 本文档供 Claude Code 执行使用。请按 Phase 顺序推进，每个 Phase 完成后对照"验收标准"自检，再进入下一阶段。
> 遇到本文档未覆盖的决策，先在 `docs/decisions.md` 记录选项和理由，再继续。

---

## 0. 背景与目标

**现状**：React 前端 + FastAPI BFF 已基本完成，目前读取 Azure SQL（部分为 mock 数据）。
**本项目**：开发 Python 数据管线，从 SEC EDGAR 和 Alpha Vantage 获取数据 → 解析/提取 → 计算指标 → LLM 生成洞察 → 写入 Azure SQL，供 BFF 读取。

**业务范围（来自 BRD《Professional 360 MVP》）**
- 业务线：D&O，Americas；仅美国上市公司；仅英文
- 数据源：SEC EDGAR（10-K / 10-Q / 8-K）、Alpha Vantage（新闻情绪、财报电话会议纪要）
- 不做：私营公司、其他 LoB/地区、sell-side 报告、聊天机器人、loss run 历史数据处理
- 原则：**不自动化核保决策**，只提供可解释、可溯源的信息

**核心质量要求**
1. **可溯源**：每条洞察、每个数值都能追溯到具体文件（accession number / URL / 章节）
2. **行业适配**：金融机构（FI）与非金融（Commercial）展示不同指标集，不能混用
3. **幂等可重跑**：同一输入重跑结果一致，不产生重复行
4. **增量更新**：只处理新文件 / 新新闻，避免重复调用 API 和 LLM

---

## 1. 前端数据需求清单（管线的输出契约）

以下按页面模块列出，管线必须产出对应数据。

| 前端模块 | 所需数据 | 主要来源 | 生成方式 |
|---|---|---|---|
| Dashboard 卡片 / 公司头部 | 名称、ticker、SIC 代码与描述、总部城市/州、logo、最后更新时间 | SEC submissions | 规则 |
| Dashboard 计数 | Risk Flags 数、Follow-Up Questions 数 | 由下游表汇总 | 规则（视图或 BFF 计算）|
| Key Insights Summary | 类型（Warning/Negative/Positive/Info）、标题、摘要、核保相关性、支撑证据、来源、关联指标/风险/问题 | 指标 + 文件文本 + 新闻 | LLM（基于结构化输入）|
| Financial Metrics | Total Revenue、Revenue Growth、Adjusted EBITDA，及公司特有指标（如 Marriott 的 Gross/Net Fee Revenue）| XBRL | 规则 + 配置 |
| Liquidity Metrics | Cash & Equivalents、Current Assets、Current Liabilities、Current Ratio、Working Capital | XBRL | 规则 |
| Leverage Metrics | Total Debt、Long-term Debt、Debt-to-Equity、Interest Expense、Interest Coverage | XBRL | 规则 |
| Operational Metrics | 行业特定：RevPAR/入住率（酒店）、ARR/流失率（SaaS）、产能利用率（制造）| 10-K/10-Q MD&A 文本 | LLM 抽取 + 校验 |
| Performance Trends（仅 FI）| NIM、CET1、NPL Ratio、Fee Income Growth | XBRL + 文本 | 规则优先，LLM 兜底 |
| Loan Portfolio Composition（仅 FI）| 分部、余额、占比、质量评级 | 10-K/10-Q 附注 | LLM 抽取 |
| Strategic & Risk Insights | 大类（Governance & Leadership / Risk & Legal / Event Signals / Behavioral Insights）、子类、标题、描述、核保相关性 | 8-K item、10-K 章节、电话会议纪要 | 规则 + LLM |
| Recent Activities | 标题、日期、摘要、情绪、核保相关性、来源链接 | Alpha Vantage NEWS_SENTIMENT | API + LLM 摘要 |
| Suggested Follow-Up Questions | 类别（Financial/Operational/Market/Strategy/Governance）、问题、依据、系统建议优先级 | 上述全部 | LLM |
| QoQ / YoY / 5Ys 切换 | 季度值（至少 2 年）、年度值（至少 5 年）| XBRL | 规则 |

**不由管线负责（BFF 自有状态）**：Favorites、Recently Viewed、用户对问题的 flag 操作、Save Preferences。

> ⚠️ 已知问题：当前 Marriott 页面展示了 NIM、CET1、Loan Portfolio 等银行指标，且 Key Insight 来源为银行类文件（IFRS 9、CRE Stress Test）——这是 mock 数据。管线必须按 `industry_group` 控制哪些模块有数据，BFF/前端在无数据时隐藏该模块。

---

## 2. 架构

```
                ┌──────────────┐     ┌────────────────┐
  SEC EDGAR ───►│              │     │  ADLS (raw)    │  原始 JSON/HTML，按日期+CIK 分区
  Alpha Vantage►│  1. Extract  ├────►│  可重放        │
                └──────┬───────┘     └────────────────┘
                       ▼
                ┌──────────────┐
                │ 2. Normalize │  XBRL facts → 标准化季度/年度事实；文件 → 章节文本
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 3. Compute   │  指标注册表 → metric_value（纯规则，无 LLM）
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 4. Signals   │  8-K item 规则、审计师/高管变更、going concern 关键词
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 5. Enrich    │  LLM：洞察、风险解读、追问问题、文本指标抽取
                └──────┬───────┘     （输入哈希不变则跳过）
                       ▼
                ┌──────────────┐
                │ 6. Load      │  staging 表 + MERGE upsert → Azure SQL
                └──────────────┘
```

**设计原则**
- 每层输出都可落地（raw → ADLS；中间结果 → staging 表或 parquet），任一层可单独重跑
- 数值只来自 Compute 层；LLM **不得**自行产生数字，只能引用 Compute 层已有的数值
- 外部调用（HTTP、LLM、DB）全部通过 `clients/` 封装，便于测试时替换

**运行形态**：OpenShift CronJob（与现有 `professional-cronjob` 一致），Harness CI/CD 部署。单次运行 = 一个 `pipeline_run`，按公司拆成 `pipeline_task`，复用 job table 模式支持断点续跑。

---

## 3. 技术选型

| 关注点 | 选择 | 备注 |
|---|---|---|
| Python | 3.11+ | |
| HTTP | `httpx` + `tenacity`（重试/退避）| 自写 token bucket 限速 |
| SEC 解析 | 优先评估 `edgartools`；不合适则自写 | 需确认 Artifactory 镜像可用 |
| HTML → 章节 | `beautifulsoup4` / `lxml` + 正则定位 Item | 见 §5.3 |
| 数据处理 | `pandas` 或 `polars` | 二选一，统一使用 |
| 数据校验 | `pydantic` v2 | 所有 LLM 输出必须过 schema |
| DB | `SQLAlchemy 2.x` + `pyodbc`（ODBC Driver 18）| `fast_executemany=True` |
| DB 认证 | Service Principal / Managed Identity | 不使用 SQL 密码 |
| LLM | 抽象 `LLMClient` 接口，实现：Secure GPT 网关 / Claude API | 由配置切换 |
| 配置 | `pydantic-settings` + 环境变量 + YAML | 密钥来自 OpenShift Secret |
| CLI | `typer` | |
| 测试 | `pytest` + 录制的响应 fixtures | 测试不访问外网 |
| Lint | `ruff` + `mypy` | |

---

## 4. 目录结构

```
uw-pipeline/
├── pyproject.toml
├── CLAUDE.md                     # 项目约定（见 §11）
├── docs/
│   ├── PLAN.md                   # 本文件
│   ├── decisions.md              # 决策记录
│   └── schema-contract.md        # Phase 0 产出：与 BFF 的表结构契约
├── config/
│   ├── companies.yaml            # 公司清单（MVP 阶段）
│   ├── concept_map.yaml          # 指标 → XBRL tag 候选列表
│   ├── metrics.yaml              # 指标定义注册表
│   └── company_overrides/        # 公司特有指标（如 MAR.yaml）
├── src/uw_pipeline/
│   ├── settings.py
│   ├── clients/
│   │   ├── sec.py                # EDGAR：限速、UA、缓存
│   │   ├── alphavantage.py
│   │   ├── llm.py                # LLMClient 抽象 + 实现
│   │   ├── storage.py            # ADLS 读写
│   │   └── db.py
│   ├── extract/                  # 拉取 + 落 raw
│   ├── normalize/
│   │   ├── xbrl_facts.py         # companyfacts → 长表
│   │   ├── periods.py            # 季度推导（含 Q4 = FY − 9M）
│   │   └── sections.py           # 10-K/10-Q 章节切分
│   ├── compute/
│   │   ├── registry.py           # 读取 metrics.yaml
│   │   └── metrics.py
│   ├── signals/
│   │   ├── eight_k.py            # 8-K item → 事件
│   │   └── governance.py         # 审计师、高管变更
│   ├── enrich/
│   │   ├── schemas.py            # pydantic 输出 schema
│   │   ├── prompts/              # 带版本号的 prompt 模板
│   │   ├── insights.py
│   │   ├── questions.py
│   │   ├── text_metrics.py       # 运营指标 / FI 指标文本抽取
│   │   └── grounding.py          # 数字与引用校验
│   ├── load/
│   │   └── upsert.py             # staging + MERGE
│   ├── jobs/
│   │   ├── orchestrator.py       # run / task 状态机
│   │   └── run_company.py
│   └── cli.py
├── sql/                          # 若 schema 由本仓库维护（见 Phase 0 决定）
└── tests/
    ├── fixtures/sec/  fixtures/av/  fixtures/llm/
    └── ...
```

---

## 5. 数据源细节

### 5.1 SEC EDGAR

**强制要求**
- 每个请求带 `User-Agent: "<公司名> <联系邮箱>"`，否则会被封
- 全局限速 ≤ 10 req/s（实现时取 8 留余量）
- CIK 一律补零到 10 位

**端点**
| 用途 | URL |
|---|---|
| ticker ↔ CIK | `https://www.sec.gov/files/company_tickers.json` |
| 公司信息 + 文件列表 | `https://data.sec.gov/submissions/CIK##########.json` |
| 全部 XBRL 事实 | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` |
| 单个概念 | `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/{Tag}.json` |
| 文件原文 | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}` |

**submissions JSON 可直接得到**：`sicDescription`、`sic`、`addresses.business`（总部）、`fiscalYearEnd`、`filings.recent`（form、filingDate、accessionNumber、primaryDocument、**items**）。注意 `filings.recent` 只含最近约 1000 条，更早的在 `filings.files` 分页。

### 5.2 XBRL → 标准化财务数据（最容易出错的部分）

**companyfacts 结构**：`facts.{taxonomy}.{Tag}.units.{unit}[]`，每条含 `start`、`end`、`val`、`fy`、`fp`、`form`、`filed`、`accn`、`frame`（可选）。

**必须处理的坑**
1. **同一期间多个值**：后续文件会重述前期数据。规则：同一 (tag, start, end) 取 `filed` 最新的值；同时保留历史以便审计
2. **期间长度**：10-Q 损益表同时有 3 个月值和 YTD 值。用 `end - start` 天数区分（季度约 80–100 天，年度约 350–380 天）
3. **Q4 无直接值**：Q4 = FY − (Q1+Q2+Q3) 或 FY − 9M YTD。标记 `derivation = DERIVED`
4. **现金流表只有 YTD**：季度值需差分，同样标记 `DERIVED`
5. **资产负债表是时点值**（无 `start`），直接取季末
6. **`frame` 字段**（如 `CY2025Q1`、`CY2025Q1I`）是 SEC 去重后的日历期间，可作为对齐辅助，但不是每条都有
7. **财年 ≠ 自然年**：存储 `fiscal_year`、`fiscal_quarter` 和 `period_end` 三者，前端按财年展示
8. **Tag 不统一**：同一指标不同公司用不同 tag，用 `concept_map.yaml` 定义有序候选列表，取第一个有数据的；记录实际使用的 tag
9. **扩展 tag**：公司特有指标（如 Marriott 费用收入）在公司自有 taxonomy 下，需在 `company_overrides/` 中配置

**concept_map.yaml 示例**
```yaml
revenue:
  - us-gaap:Revenues
  - us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax
  - us-gaap:SalesRevenueNet
net_income:          [us-gaap:NetIncomeLoss]
operating_income:    [us-gaap:OperatingIncomeLoss]
d_and_a:
  - us-gaap:DepreciationDepletionAndAmortization
  - us-gaap:DepreciationAndAmortization
  - us-gaap:DepreciationAmortizationAndAccretionNet
cash:
  - us-gaap:CashAndCashEquivalentsAtCarryingValue
  - us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents
current_assets:      [us-gaap:AssetsCurrent]
current_liabilities: [us-gaap:LiabilitiesCurrent]
long_term_debt_noncurrent: [us-gaap:LongTermDebtNoncurrent]
long_term_debt_current:    [us-gaap:LongTermDebtCurrent]
short_term_borrowings:     [us-gaap:ShortTermBorrowings, us-gaap:CommercialPaper]
equity:
  - us-gaap:StockholdersEquity
  - us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest
interest_expense:
  - us-gaap:InterestExpense
  - us-gaap:InterestExpenseNonoperating
  - us-gaap:InterestExpenseDebt
operating_cash_flow: [us-gaap:NetCashProvidedByUsedInOperatingActivities]
capex:               [us-gaap:PaymentsToAcquirePropertyPlantAndEquipment]
buybacks:            [us-gaap:PaymentsForRepurchaseOfCommonStock]
dividends_paid:      [us-gaap:PaymentsOfDividends, us-gaap:PaymentsOfDividendsCommonStock]
auditor_name:        [dei:AuditorName]   # 2021 年后 10-K 封面
```
> 上表为起点，Phase 2 需用实际公司数据验证每个 tag 的覆盖率，并输出覆盖率报告。

### 5.3 文件文本 → 章节

**10-K 需要的章节**：Item 1A（Risk Factors）、Item 3（Legal Proceedings）、Item 7（MD&A）、Item 9A（Controls）、财务报表附注中的 Commitments & Contingencies、审计报告（审计师任期 "served as auditor since"、going concern 段落）。
**10-Q**：Part I Item 2（MD&A）、Part II Item 1（Legal）、Item 1A（Risk Factors 更新）。

**实现要求**
- 先尝试 `edgartools` 的章节解析；不稳定时用 HTML 文本 + Item 标题正则，跳过目录页（第一次出现通常是目录）
- 存储：`filing_section`（accession、section_code、text、char_count、hash）
- 文本过长时按段落分块，每块保留 `section_code` 和偏移量，用于 LLM 引用定位

### 5.4 8-K 事件信号（纯规则，无 LLM）

submissions JSON 的 `items` 字段直接给出 8-K item 编号：

| Item | 含义 | 前端分类 | 默认严重度 |
|---|---|---|---|
| 1.01 | 签订重大协议 | Event Signals – Material Agreement | Info |
| 1.03 | 破产或接管 | Event Signals – Bankruptcy | **Critical** |
| 2.01 | 完成收购/处置 | Event Signals – M&A | Info |
| 2.04 | 触发加速偿债义务 | Event Signals – Debt | Warning |
| 2.06 | 重大减值 | Event Signals – Impairment | Warning |
| 3.01 | 退市通知 | Event Signals – Listing | Warning |
| 4.01 | 更换审计师 | Governance – Auditor Change | Warning |
| 4.02 | 财报不可依赖（重述）| Event Signals – Restatement | **Critical** |
| 5.02 | 董事/高管离任或任命 | Governance – Leadership Change | 视职位而定 |

5.02 需读取 8-K 正文判断是否涉及 CEO/CFO（可用关键词规则，LLM 仅做摘要）。

### 5.5 Alpha Vantage

| 用途 | function | 关键参数 |
|---|---|---|
| 新闻 + 情绪 | `NEWS_SENTIMENT` | `tickers`、`time_from`（YYYYMMDDTHHMM）、`limit`（≤1000）、`sort` |
| 电话会议纪要 | `EARNINGS_CALL_TRANSCRIPT` | `symbol`、`quarter`（如 `2026Q1`）|
| 公司概况（备用）| `OVERVIEW` | `symbol` |
| 财报（交叉校验）| `INCOME_STATEMENT` / `BALANCE_SHEET` / `CASH_FLOW` | `symbol` |

**要点**
- 确认订阅档位对应的限速，在 `settings` 中配置；免费档每日额度极低，不适合生产
- 新闻按 `ticker_sentiment[].relevance_score` 过滤（阈值可配置，初始 0.3），避免一篇泛泛提到多家公司的文章被归到每家公司
- 去重：URL 归一化后哈希
- AV 的情绪分数直接存储；核保相关性摘要由 LLM 生成
- AV 财报数据仅用于和 XBRL 交叉校验，差异超过阈值写入 DQ 报告，**不作为展示主数据源**

---

## 6. 指标计算规则

所有指标在 `config/metrics.yaml` 中注册：`code`、`display_name`、`description`、`section`、`unit`、`applies_to`（ALL / FI / NON_FI / 行业代码）、`formula`、`inputs`。

| 指标 | 公式 | 特殊情况 |
|---|---|---|
| Revenue Growth (QoQ/YoY) | (本期 − 比较期) / \|比较期\| | 比较期为 0 或缺失 → NULL |
| EBITDA（计算值）| Operating Income + D&A | 标注 "computed"；**Adjusted EBITDA 是非 GAAP，不在 XBRL 中**，见下 |
| Current Ratio | Current Assets / Current Liabilities | |
| Working Capital | Current Assets − Current Liabilities | 允许负数 |
| Total Debt | LTD 非流动 + LTD 流动 + 短期借款 | 记录实际组成部分 |
| Debt-to-Equity | Total Debt / Equity | **Equity ≤ 0 时返回 N/M 并生成风险信号**（Marriott 即为负权益）|
| Interest Coverage | EBIT / Interest Expense | 利息为 0 → NULL |
| Free Cash Flow | OCF − Capex | 季度值需差分 |
| Buyback 强度 | Buybacks / Net Income | 配合负权益生成治理类信号 |

**Adjusted EBITDA**：两种方案，Phase 2 实现方案 A，Phase 5 视需要实现方案 B，前端标签需区分。
- A：展示计算 EBITDA，标签为 "EBITDA (computed)"
- B：从 8-K EX-99.1（财报新闻稿）用 LLM 抽取公司公布的 Adjusted EBITDA，`derivation = LLM_EXTRACTED`，必须附原文片段

**FI 指标**：Tier 1 / CET1 / LCR / NIM / NPL 部分有 us-gaap tag 但覆盖不稳定。流程：先尝试 XBRL → 无则从 MD&A 文本用 LLM 抽取 → 抽取值必须附原文片段，且数值必须在原文中出现（字符串匹配校验）。

**行业分组**：用 SIC 代码判定 `industry_group`。FI = SIC 6000–6299（银行、信贷、券商）及 6700 段中适用的控股公司，具体清单写入配置并在 Phase 1 确认。保险（63xx）是否归入 FI 待业务确认。

---

## 7. LLM 增强层

### 7.1 通用规则
- 输入为**结构化包**：已计算的指标表（含期间与来源）、信号列表、相关章节片段、新闻摘要。不把整本 10-K 塞给模型
- 输出必须是 JSON，经 pydantic 校验；失败则带错误信息重试一次，再失败则记录并跳过
- 每条输出必须包含 `evidence[]`：`{doc_id, section_code, snippet}`，snippet 必须能在源文本中找到（模糊匹配阈值可配置）
- **数字校验**（`grounding.py`）：抽取输出文本中的所有数字，必须能在输入指标表或引用片段中找到（允许单位换算和四舍五入）。不通过则丢弃该条并记日志
- 记录 `model`、`prompt_version`、`input_hash`、token 用量
- `input_hash` 未变化则跳过 LLM 调用，直接沿用上次结果

### 7.2 任务清单

| 任务 | 输入 | 输出 schema 要点 |
|---|---|---|
| Key Insights | 指标 + 信号 + 新闻 + 风险因素摘要 | 6–10 条；`type`、`title`（≤8 词）、`summary`、`uw_relevance`、`supporting_evidence`、`related_metric_codes[]`、`evidence[]` |
| Strategic & Risk Insights | 8-K 事件、治理信号、Item 1A/3、电话会议纪要 | `category`、`subcategory`、`title`、`description`、`uw_relevance`、`evidence[]`；允许 "no signal" 结论 |
| Behavioral Insights | 最近 2–4 期电话会议纪要 | 语气/情绪变化、战略重心转移、新出现的风险表述、措辞变化（如 "restructuring"、"challenging macro"）|
| Follow-Up Questions | 上述所有洞察 | 10–15 条；`category`、`question`、`rationale`、`system_priority`（high = 默认标记）、`related_insight_ids[]`；需体现行业特征（SaaS→流失率/安全；酒店→入住率；FI→资本充足率）|
| Recent Activities 摘要 | AV 新闻 | `summary`、`uw_relevance`、`sentiment` |
| 文本指标抽取 | MD&A 片段 | `metric_code`、`value`、`unit`、`period`、`snippet` |

### 7.3 Prompt 管理
- 存放于 `enrich/prompts/{task}_v{N}.md`，变更即升版本号
- 每个任务至少准备 3 家公司的 golden 输出（Marriott、JPMorgan、Enphase 覆盖酒店/FI/科技），用于回归对比

---

## 8. 数据库设计（草案，Phase 0 需与 BFF 对齐）

> 如果 BFF 已有表结构，**以 BFF 为准**，本节仅作差距分析参考。Schema 迁移只能有一个负责方（BFF 仓库或本仓库），Phase 0 确定。

**参考维度**
- `company`：company_id、cik、ticker、name、sic_code、sic_description、industry_group、hq_city、hq_state、fiscal_year_end、logo_url、is_active、last_refreshed_at
- `metric_definition`：对应 metrics.yaml

**事实与来源**
- `source_document`：doc_id、company_id、source_type（SEC_FILING / NEWS / TRANSCRIPT）、form_type、accession_no、filed_at、period_end、url、title、content_hash、raw_path、fetched_at
- `filing_section`：section_id、doc_id、section_code、text、hash
- `xbrl_fact`（staging / 审计）：company_id、taxonomy、tag、unit、val、start_date、end_date、fy、fp、form、accession_no、filed_at、frame
- `metric_value`：company_id、metric_code、period_type（Q / FY）、fiscal_year、fiscal_quarter、period_end、value、unit、derivation（REPORTED / DERIVED / COMPUTED / LLM_EXTRACTED）、source_tag、source_doc_id、run_id
  - 唯一键：(company_id, metric_code, period_type, fiscal_year, fiscal_quarter)

**洞察类**
- `insight`：insight_id、company_id、kind（KEY / STRATEGIC_RISK）、category、subcategory、severity、title、summary、uw_relevance、supporting_evidence、event_date、is_current、model、prompt_version、input_hash、run_id、created_at
- `insight_evidence`：insight_id、doc_id、section_code、snippet
- `insight_metric`：insight_id、metric_code
- `activity`：activity_id、company_id、title、published_at、source_name、url、url_hash、av_sentiment_score、av_sentiment_label、av_relevance_score、summary、uw_relevance
- `followup_question`：question_id、company_id、category、question、rationale、system_priority、is_current、run_id
- `followup_question_insight`：question_id、insight_id
- `loan_portfolio_segment`（仅 FI）：company_id、period_end、segment、outstanding、pct_of_book、quality、source_doc_id

**运行管理**
- `pipeline_run`：run_id、trigger、started_at、finished_at、status、stats_json
- `pipeline_task`：task_id、run_id、company_id、stage、status、attempts、error、updated_at

**版本策略**：洞察类表用 `is_current` 软切换——新结果写入后在同一事务内把旧结果置为 `is_current = 0`，BFF 只读 current，保留历史以便审计和对比。

**写入方式**：批量写 staging 表 → `MERGE` 到目标表，单公司单阶段一个事务。

---

## 9. 编排与运行

**CLI**
```
uw-pipeline run --all                    # 全量公司，增量模式
uw-pipeline run --ticker MAR --stage compute
uw-pipeline run --ticker MAR --force     # 忽略缓存与 input_hash
uw-pipeline backfill --ticker MAR --years 5
uw-pipeline dq-report --run-id <id>
```

**调度建议（OpenShift CronJob）**
| 任务 | 频率 | 内容 |
|---|---|---|
| filings-sync | 每日 | 检查 submissions 新 accession → extract/normalize/compute/signals |
| news-sync | 每日（或每 6 小时）| AV 新闻增量 |
| enrich | filings/news 有变化后 | 只对变化公司跑 LLM |
| transcripts | 每周 | 财报季可提高频率 |

**增量判断**：`source_document` 中已存在的 accession_no / url_hash 跳过；companyfacts 用 content_hash 判断是否变化。

**部署**：Dockerfile（基础镜像来自 Artifactory）、Harness pipeline、OpenShift Secret 存放 SEC UA 邮箱、AV key、LLM key、DB 凭据。

---

## 10. 分阶段计划

### Phase 0 — 契约对齐（先做，不写管线代码）
- [ ] 阅读 BFF 仓库：SQLAlchemy 模型 / SQL 查询 / Pydantic DTO / mock 数据文件
- [ ] 产出 `docs/schema-contract.md`：BFF 实际读取的每张表、每个字段，对照 §1 和 §8 列出差距
- [ ] 确认 schema 迁移归属（BFF 仓库 or 本仓库）
- [ ] 列出前端各模块在"无数据"时的预期行为

**验收**：schema-contract.md 经人工确认；差距项都有处理方案。

### Phase 1 — 骨架 + SEC 客户端 + 公司主数据
- [x] 项目骨架、settings、CLI、ruff/mypy/pytest 配置、CLAUDE.md
- [x] `clients/sec.py`：UA、限速、重试、本地/ADLS 缓存
- [x] `companies.yaml`：初始清单 = 前端已出现的公司（MAR、LUV、F、PFE、JPM、M、LUMN、PARA、ENPH）
- [x] ticker → CIK → submissions → 写入 `company`；判定 `industry_group`
- [x] raw 数据落 ADLS（暂落本地 `data/raw/`，见 decisions D-003）

**验收**：`uw-pipeline run --all --stage company` 后 `company` 表 9 行，字段完整；单测不访问外网。

### Phase 2 — XBRL 财务指标
- [x] companyfacts → `xbrl_fact` 长表（companyfacts 缺失的近期文件改读 XBRL instance，见 decisions D-013）
- [x] `periods.py`：期间长度分类、重述去重、Q4 推导、现金流差分
- [x] `concept_map.yaml` + 覆盖率报告（每家公司每个指标用了哪个 tag / 缺失）：`uw-pipeline coverage-report`，说明见 `config/coverage_notes.yaml`
- [x] `metrics.yaml` + compute：Financial / Liquidity / Leverage 全部指标，至少 5 个财年 + 8 个季度
- [x] Marriott 公司特有指标（费用收入）的 override 配置
- [x] 写入 `metric_value`

**验收**：
- Golden test：Marriott 最近一个 10-Q 中 Total Debt、Cash、Revenue 与文件原值一致（Total Debt ≈ $16.5B、Cash ≈ $0.5B，以实际文件为准）
- Q4 推导值与 10-K 全年值 − 前三季度之和一致
- Marriott D/E 输出 N/M（负权益）
- 覆盖率报告中每家公司核心指标缺失项都有说明

### Phase 3 — 文件文本 + 规则信号
- [x] 10-K / 10-Q 主文档下载与章节切分 → `filing_section`（自写切分，未用 edgartools，见 decisions D-023；财报 EX-99.1 一并下载，D-026）
- [x] 8-K item 规则 → 事件信号（§5.4）
- [x] 治理信号：审计师（dei:AuditorName + 审计报告任期）、4.01 审计师变更、5.02 CEO/CFO 变更
- [x] 关键词信号：going concern、material weakness、restatement
- [x] 判断类信号改为 规则 + LLM 投票（Sonnet 5.5 抽取，分歧由 Opus 5.5 裁决；见 decisions D-028）

**验收**：9 家公司最近一份 10-K 的 Item 1A / 3 / 7 切分成功率 ≥ 90%，失败的有日志；最近 2 年 8-K 事件全部入库。

### Phase 4 — Alpha Vantage
- [x] `clients/alphavantage.py`：按订阅档限速、重试、缓存（含每日额度计数、错误消息中 key 脱敏）
- [x] 新闻增量拉取、相关性过滤、去重 → `activity`（摘要字段留空，Phase 5 填）：三层去重见 decisions D-029；NEWS_SENTIMENT 需付费 key，真实数据验收待定
- [x] 电话会议纪要拉取（最近 4 个季度）→ `source_document`（TRANSCRIPT）（D-030）
- [ ] ~~AV 财报与 XBRL 交叉校验 → DQ 报告~~ 暂不做（用户决定，D-031）

**验收**：重复运行不产生重复 activity；每家公司至少有近 30 天新闻（若 AV 有）。

### Phase 5 — LLM 增强
- [ ] `LLMClient` 抽象 + Secure GPT / Claude 实现（抽象与 Claude 实现已完成，D-028；Secure GPT 待接入）
- [x] `schemas.py`、prompts v1、`grounding.py`（引用 + 数字校验）（D-033）
- [x] Key Insights、Strategic & Risk Insights、Behavioral Insights、Follow-Up Questions、Activity 摘要（D-033；Activity 摘要待付费新闻数据）
- [x] 文本指标抽取：酒店 RevPAR/入住率；FI：CET1/NIM/NPL/贷款组合（D-032）
- [x] 财报 8-K（Item 2.02）EX-99.1 抽取核心财务数字 → `derivation = PRELIMINARY`，对应期间 10-Q/10-K 入库后自动被替换（隔夜更新，见 decisions D-022）
- [x] input_hash 缓存（`llm_call`，D-028），`is_current` 版本切换（D-033）

**验收**：
- 3 家 golden 公司输出全部通过 schema 校验和 grounding 校验
- 随机抽查 20 条洞察，引用片段 100% 可在原文找到
- 非 FI 公司不产生 FI 模块数据
- 第二次运行（输入不变）LLM 调用次数为 0

### Phase 6 — 编排、部署
- [x] `pipeline_run` / `pipeline_task` 状态机，失败重试与断点续跑（D-034；kill -9 后重启续跑已测试）
- [x] Dockerfile、Harness pipeline、OpenShift CronJob 清单（镜像源、namespace、Harness 细节为占位符）
- [x] 结构化日志（JSON），运行统计写入 `stats_json`

**验收**：dev 环境 CronJob 连续 3 天成功运行；手动杀掉 pod 后重跑能从未完成的 task 继续。

### Phase 7 — 数据质量与上线准备
- [x] DQ 规则：指标缺失率、季度值跳变异常（如环比 > 500%）、~~XBRL vs AV 差异~~（不取 AV 财报，D-031）、LLM 校验失败率（D-035，另含新闻稿 vs XBRL、季度合计 vs 全年等）
- [ ] BFF 联调：所有模块读真实数据，移除 mock（待 BFF 仓库；对照草案见 `docs/schema-contract.md`）
- [x] 运维文档：如何新增公司、新增指标、升级 prompt（`docs/OPERATIONS.md`）

**验收**：前端 9 家公司页面全部显示真实数据，无 mock 残留。

### Phase 8 — 前端详情弹窗数据（2026-10-13 新增）

**背景**：前端有五种详情弹窗，都由卡片上的灯泡按钮打开，目前显示的全是 mock 数据（例如 Marriott 的 Total Revenue 弹窗里写的是 RetailCo 的 FCCR）。本阶段让管线为每个弹窗、每个 tab 产出真实、可溯源的数据。

**核心设计**：把指标、洞察（Key / Strategic & Risk / Behavioral）、追问问题三类对象之间的关联全部补齐，并做到双向可查。五种弹窗共用这套关联，以及同一套"来源标题和链接"。

| 弹窗 | 左侧内容 | 右侧 tab |
|---|---|---|
| 指标（各指标卡片、Performance Trends 图表） | Formula / Evidence Methodology、Data Sources、Underwriting Relevance | Related Key Insight / Risks / Followup Questions |
| Loan Portfolio Composition（仅 FI） | Methodology、Data Sources、Raw Data Points | — |
| Strategic & Risk / Behavioral 洞察 | Category、Risk Type、Updated At、Summary、UW Relevance、Supporting Evidence、Sources | Related Key Insight / Metrics / Followup Questions |
| Key Insight | 同上 | Related Risks / Metrics / Followup Questions |
| Follow-Up Question | Category、Question、Summary、Following Risk、UW Relevance、Sources | Related Key Insight / Metrics / Risks |

**生成方式的原则**：能用规则生成的一律用规则（Methodology、Data Sources、Supporting Evidence、Sources、Raw Data Points），保证可追溯、零 LLM 费用。只有需要判断的内容才调用 LLM（指标的核保相关性、关联选择、追问问题的补充字段），并照常走 schema 校验和数字校验。

#### 8.1 基础：来源标题与洞察关联（不调用 LLM）
- [x] `config/source_labels.yaml`：章节代码对应的可读名称，例如 8K-5.02 是 "Item 5.02 – Departure of Directors or Officers"，10K-1A 是 "Risk Factors"，TRANSCRIPT 是 "Earnings Call"。标题格式为"公司名 + 表单 + 提交日期 + 章节"，并附 SEC / AV 链接
- [x] `insight_link` 表：记录 Key Insight 引用了哪些 R/B 洞察，生成时写入；现有数据从缓存的 LLM 输出里重新解析
- [x] `insight` 表增加 `updated_at`（优先用 `event_date`，否则用所引用文件里最新的提交日期）和 `supporting_evidence_text`（规则生成：出处加原文片段）

#### 8.2 FI 数据补齐（Performance Trends、Loan Portfolio）
- [x] Fee Income Growth：新增 `noninterest_income`（XBRL 标准 tag `NoninterestIncome`）和 `fee_income_growth_yoy`，加入银行组的指标清单
- [x] NIM / CET1 / NPL 补齐约 5 年历史：仅对 FI 公司，额外回溯最近 5 份 10-K 的 MD&A 来抽取，仍要求双模型一致；年数写在配置里
- [x] 贷款组合改为按贷款类别抽取（约 6–8 行），每行包括所属大板块、余额、占比、不良贷款和不良率，另加上一年末的数据作对比
- [x] `loan_portfolio_detail`：Methodology、Data Sources、Raw Data Points（原文表格行加上从数据算出的要点），全部规则生成

#### 8.3 追问问题升级（prompt `followups_v2`）
- [x] 输出增加 `summary`（背景事实）、`following_risk`（问题针对的风险领域）、`uw_relevance`、`related_metric_codes`；数字照常校验
- [x] Sources：规则生成，汇总所关联洞察的证据文件和相关指标的数据来源
- [x] `followup_question_metric` 表

#### 8.4 指标详情（新增 `metricdetail` 阶段，排在 insights 之后）
- [x] Methodology（规则模板）：公式或概念、实际用到的 XBRL tag、各期取值方式（直接报告 / 推导 / 公式计算 / 文本抽取 / 初步数字）、N/M 条件、最新值；文本类指标附口径定义和原文片段
- [x] `metric_source`（规则）：最近一个季度和最近一个财年的值分别来自哪份文件，包括报表类型、提交日期和链接
- [x] LLM（每家公司一次 Opus 调用）：为每个有数据的指标写 `uw_relevance`，并选出相关的 K / R / B / Q 编号；编号必须真实存在，数字必须通过校验
- [x] `metric_insight` 表（带 `is_current`）；指标与洞察、问题的关联并入 `insight_metric` 和 `followup_question_metric`

#### 8.5 契约、质量与验收工具
- [x] `docs/schema-contract.md`：逐个弹窗、逐个 tab 写明读哪张表、用什么条件；Performance Trends 和 Loan Portfolio 只在 `is_financial = 1` 时显示
- [x] 新增 DQ 规则：有数据的展示指标必须有 methodology 和来源；每条洞察和问题至少有一个来源；关联必须指向当前版本的对象
- [x] `audit-insights` 扩展到指标详情和追问问题的引用检查；更新 golden 输出

**费用估算**：8.4 每家公司每次重新生成约 $0.3–0.5，只在指标或洞察变化时发生；8.2 的历史回溯一次性约 $2–3（JPM）；8.3 的追问问题重新生成约 $1.6；其余不调用 LLM。

**验收**：
- 9 家公司中，所有有数据的展示指标 100% 有 Methodology 和 Data Sources，≥ 90% 有 Underwriting Relevance
- 每条当前版本的洞察和追问问题都有至少一个可点击的来源；Supporting Evidence 的片段 100% 能在原文找到
- 关联双向一致：从 A 能查到 B，从 B 也能查到 A，并且都指向当前版本
- JPM 的 NIM / CET1 / NPL 覆盖至少 5 个财年；贷款组合有类别级的行和不良率
- 非 FI 公司没有 Performance Trends 和 Loan Portfolio 的数据
- 输入不变时重跑，LLM 调用为 0

---

### Phase 9 — 写入应用库、新闻质量、风险评分（2026-10-13 新增）

**背景**：团队的数据模型图（应用库）就是 BFF 读的表。图的左半部分由管线取代；右半部分由新的 `publish` 阶段写入，并按详情弹窗的需要做少量改动（D-037）。

- [x] 9.1 应用库表定义 `schema_app.py`：按数据模型图，加上 `ItemLink`、`CompanyMetricDetail` 两张新表和少量新列；`docs/schema-contract.md` 改为应用表的对照
- [x] 9.2 `publish` 阶段：应用 id 稳定（`pipeline_publish_map`）、内容不变不写、版本化对象用 isActive、已删除的行会补回；RiskTypes 和 confidence 按 `config/app_publish.yaml`
- [x] 9.3 新闻（付费 key）：回补 30 天；规则 + LLM 判断只保留与公司本身有关的新闻；`uw-news` CronJob 启用（03/09/15/21 点）
- [x] 9.4 风险评分（Sonnet 5.5）：`NewsRiskScore`、`TranscriptToneAnalysis`、`TranscriptRiskFlag`；只有重要新闻（overall ≥ 0.5 或任一维度 ≥ 0.6）进入洞察输入
- [x] 9.5 模型切换（用户决定，D-038）：抽取用 Haiku 5.5，复核和综合用 Sonnet 5.5；全部 LLM 任务按新模型重跑一次，并与旧结果对比质量。`scripts/compare_models.py` 留作以后对比用

**验收**：
- 9 家公司全部写入应用库；无变化时重跑写入 0 行
- 版本更新后旧行 isActive = 0，id 不复用；ItemLink 只连接 isActive 的行
- 新闻只保留与公司本身有关的；评分有理由且数字可核对；电话会议的风险标记都是原文引用

---

### Phase 11 — 改用 SecureGPT（2026-10-14）

- [x] SecureGPT 客户端（token、结构化输出、重试、错误分类），与现有接口一致；模型改为 gpt-5-nano / gpt-5-mini（D-039）
- [x] 凭证只用环境变量；网关配置 `config/securegpt.yaml`；费用按 EUR 统计
- [x] 单元测试（模拟网关）和所有输出 schema 的 strict-mode 检查
- [ ] 在工作网络跑 `scripts/securegpt_smoke.py`，确认 `reasoning_effort` 是否可用
- [ ] 全量重跑并与当前结果对比质量（文本指标、信号、洞察丢弃率），按结果调整 effort 或模型

---

### Phase 12 — Azure SQL 和 ADLS（2026-10-14）

- [x] Azure SQL 连接（服务主体，证书校验），管线表和应用表分两个 schema；配置在 `config/azure.yaml`（占位值，待填写）
- [x] ADLS 存储（与本地存储同一套 key，原子写入）
- [x] SQL Server 兼容性：本地 Docker SQL Server 上跑通全部 220 个测试；修复镜像里的 ODBC 驱动安装
- [x] 两个 schema 的建表脚本（`deploy/sql/`），在 SQL Server 上验证可执行、可重复执行
- [ ] 填写 `config/azure.yaml` 的真实服务器、数据库、schema、存储账户、容器
- [ ] 工程师执行建表脚本；在公司网络跑 `scripts/azure_smoke.py`，然后第一次正式运行

---

### Phase 13 — 结构化数据优先、恒等式校验、薪酬与业绩（2026-10-14，D-042）

- [x] 银行资本比率（CET1、Tier 1、总资本）改从 10-K/10-Q 的 XBRL 取；监管门槛概念一律拒绝；文本抽取只补 XBRL 没有的期间，其余做交叉核对
- [x] 恒等式：RevPAR = ADR × 入住率；CET1 ≤ Tier 1 ≤ 总资本；贷款分类合计 ≤ 总贷款。文本值不成立就丢弃，DQ 报告
- [x] DEF 14A 薪酬与业绩（ecd）：CEO 薪酬、其他高管平均、公司和同业 TSR；进洞察输入，不作为指标行发布；新增"CEO 薪酬上涨而 TSR 下降"信号
- [x] 测试：真实申报节选的 golden 值、门槛对抗测试、按人标注的 CEO 薪酬、恒等式和信号

---

### Phase 10 — 扩展到约 4,000 家公司：分层与并行处理（2026-10-13 计划，未开始）

**已定**：分层处理，约 800 家全量（Tier 1），约 3,200 家轻量（Tier 2，洞察按需生成）。本阶段先只做设计，不抓取、不抽取；实现前用模拟客户端做压测。

#### 10.A 工作单元和依赖
- 工作单元仍是 (公司, 阶段) 任务，沿用 `pipeline_task` 和 `orchestrator.DEPENDS`。公司之间互不依赖，可以完全并行；同一公司内按依赖顺序执行（news / scores 与 documents / textmetrics 两条线可以同时跑）。
- 每次运行先由**协调器**根据 EDGAR 每日索引和新闻、按需请求，决定哪些公司要处理，写入任务；之后由**多个 worker** 并发领取执行。协调器很轻，只负责发现变化和建任务。

#### 10.B 领取任务（替代现在的单进程循环）
- 任务表增加 `lane`、`priority`、`claimed_by`、`lease_until`、`ready_at`。
- worker 用行锁原子领取一个"依赖已满足、未被领取或租约已过期"的任务：Postgres 用 `FOR UPDATE SKIP LOCKED`，SQL Server 用 `WITH (UPDLOCK, READPAST, ROWLOCK)`。
- 租约代替现在的整次运行心跳：worker 执行中定期续租；进程或 pod 死掉后租约过期，任务自动被别的 worker 接手。各阶段的写入已经是幂等的，重做是安全的。
- 优先级：按需请求 > Tier 1 > Tier 2；同一层里，当天有新财报的公司优先。
- 超过 `max_attempts` 的任务进入失败队列，在 DQ 和运维面板显示，不影响其他公司。

#### 10.C 按瓶颈分通道（lane）
不同阶段卡在不同的外部限制上，分开控制并发：

| 通道 | 阶段 | 瓶颈 | 并发方式 |
|---|---|---|---|
| sec | company、facts、documents | SEC 每秒 10 次（所有 pod 共用一个 IP / User-Agent） | 少量 worker + 全局令牌桶（8/秒） |
| av | news、transcripts | AV 每分钟配额 | 全局令牌桶 |
| llm-fast | signals、scores、news 相关性、textmetrics 第一遍（Haiku） | Anthropic 每分钟请求数和 token 数 | 较多 worker + 按模型的令牌桶 |
| llm-synth | insights、metricdetail、textmetrics 复核（Sonnet） | 同上；夜间改走 Batch API | 见 10.F |
| cpu | compute、publish、dq | 数据库 | 不限，按数据库连接数 |

部署上每个通道是一组 worker（OpenShift Job 并行或 Deployment），数量写在配置里；另有一组常驻小 worker 只处理按需请求（每分钟轮询）。

#### 10.D 全局限速
- 令牌桶放在数据库表里（`rate_bucket`：名称、剩余令牌、更新时间），用一条原子 UPDATE 取令牌，所有 pod 共用；以后需要更高吞吐再换 Redis。
- 收到 429 或 Anthropic 的剩余额度响应头偏低时，按桶整体退避，而不是每个 worker 各自重试。

#### 10.E 必须先改的代码结构
- **事务缩短**：现在每个阶段把一家公司的网络和 LLM 调用都放在同一个数据库事务里，可能持续几分钟。改成"先读输入（短事务）→ 在事务外调用 SEC / AV / LLM → 一次性写结果（短事务）"。这是并行的前提，也是改动最大的一步。
- **共享查找表的并发写**：publish 时多个公司同时插入同一个 SIC、指标、RiskType、LoanType，会撞同一个映射键。改为在协调器里先发布这些共享查找表，公司任务只读；并给映射插入加冲突重试。
- **LLM 缓存的并发写**：两个 worker 偶尔同时算同一个输入哈希，插入改为"冲突即忽略并读取已有结果"。
- **原始文件**：本地目录换成 ADLS（所有 pod 共享），保持现有的键规则。
- **数据库**：SQLite 无法并发写，必须换 Azure SQL；在拿到权限前用本地 Postgres 开发和压测。

#### 10.F LLM 的 Batch 化（夜间）
- Sonnet 综合任务在夜间作业中拆成两步：提交（把一批公司的请求打包提交 Batch API）和收取（结果回来后做 schema、grounding 校验并写库）。中间状态记在任务表里，worker 不必等待。
- 半价；结果通常一小时内返回，夜间作业足够。按需请求和白天的任务仍走实时 API。

#### 10.G 容量估算（高峰日，约 500 家公司有新财报）
- SEC：约 500 × 30 次请求 = 1.5 万次，8 次/秒约 30 分钟。
- Haiku 任务：并发高，约 30 分钟。
- Sonnet 综合：约 2,000 次调用；实时 16 个并发约 1.5–2 小时，或 Batch 提交后约 1 小时内返回。
- 结论：23:30 开始，约 03:00 前完成，满足次日早上更新的要求；早上 05:30 的补跑处理失败项和晚到的申报。
- 首次回补 4,000 家：companyfacts / submissions 用 SEC 批量包，不逐家请求；文件下载约 6 万次请求，约 2 小时；Tier 1 的洞察用 Batch。

#### 10.H 监控
- 运维面板：各通道队列长度、吞吐、限速等待时间、失败队列、每次运行的费用。
- 告警：队列在早上 05:00 仍未清空、失败率超阈值、单日费用超预算。

#### 10.I 实施顺序和压测（不碰真实数据源）
1. 任务表加租约和通道，worker 领取逻辑（Postgres，单元测试覆盖并发领取和租约过期）
2. 事务缩短，逐个阶段改造
3. 全局令牌桶
4. 共享查找表和缓存的并发写
5. Batch API 两步任务
6. 压测：用模拟的 SEC / AV / LLM 客户端（带延迟和随机 429），按 100 → 500 → 4,000 家公司逐级运行，验证吞吐、无重复写、kill worker 后能接续
7. 部署清单：按通道的 worker 组和常驻按需 worker

**待确认**：公司范围（哪 4,000 家）、Tier 1 名单的来源、开发期能否用本地 Postgres、按需生成由谁触发（BFF 写请求表，或管线读 `UserHistory`）。

---

## 11. 给 Claude Code 的工作约定（写入 CLAUDE.md）

- 每个 Phase 开始前先列出要改动的文件和计划，等待确认后再动手
- 单测不得访问外网；新增外部调用必须同时加 fixture
- 新的 XBRL tag 映射、指标公式、行业分组规则一律写进 `config/`，不硬编码
- LLM 输出永远不能直接写库，必须经过 schema + grounding 校验
- 数据库写入必须幂等；禁止 `DELETE` 全表后重插
- 不修改 BFF 仓库，发现契约问题记录到 `docs/schema-contract.md`
- 密钥只从环境变量读取，不写入代码、日志或 fixture
- 每完成一个 checklist 项，更新本文件对应的勾选框

---

## 12. 待确认问题

1. BFF 当前表结构与本文 §8 的差异有多大？schema 迁移由哪个仓库负责？
暂时不用管
2. Alpha Vantage 订阅档位与限速？
现在用 free key（存于本地 `.env` 的 `ALPHAVANTAGE_API_KEY`）。之后是75call/min
3. LLM 使用 Secure GPT 网关还是直接调用 Claude API？模型与 token 预算？
之后用SecureGPT，现在先用Claude API
4. 保险公司（SIC 63xx）是否按 FI 处理？
每个SIC在yaml里分开计划，可以同一个SIC用同一批metrics
5. Adjusted EBITDA 展示计算值即可，还是必须用公司公布值？
计算值可以
6. MVP 公司清单最终规模？（影响限速、LLM 成本、调度时长）
10个公司/SIC
7. 原始数据保留期限及 ADLS 路径规范？
先在本地存一下，公司的ADLS现在没有access
8. 运行在 OpenShift CronJob 还是 Databricks job？（本计划默认 OpenShift）
CronJob，这里不用管
9. Logo 来源（第三方服务、手动上传、还是 BFF 已有）？
已有