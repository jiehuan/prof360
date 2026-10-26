# 前端接口清单：需要确认的事项和改动意见

对照前端的接口清单（Index Page / Detail Page）和管线写入的应用表
（`deploy/sql/app_schema_full.sql`，读取约定见 `schema-contract.md`），2026-10-23。

分三类：A 不定下来前端没法做完；B 字段对齐；C 行为约定。

## A. 接口缺失或未定义

### A1. Key Takeaways 没有接口
公司页 Key Insights 上方的要点框（2–3 条，对专业责任险最重要的要点，不带链接）数据在
`KeyTakeaways` 表，清单里没有对应接口。

**建议**：`CompanyDetail` 加 `keyTakeaways: string[]`，按 `Ordinal` 排序，只取
`isActive = 1`；或单开 `GET api/companies/{id}/keyTakeaways`。管线保证每家公司最多 3 条。

### A2. 指标接口的 `data: object` 没定义
指标卡片和指标弹窗都依赖这个字段。**建议**拆成两种用法：

- **卡片**：一次取当前公司所有已选指标，避免一个指标一次请求。
  `GET api/companies/{id}/metrics?view=quarterly|annual`，每个指标返回：

  ```
  { id, name, categoryId, categoryName, title, subtitle, unit,
    points: [{ period: "FY2026 Q2", periodEnd, value, status: "OK"|"NM", sourceLabel }] }
  ```

  季度视图返回最近 8 个季度，年度视图返回最近 5 个财年。
- **弹窗**：`GET api/companies/{id}/metrics/{metric_id}`，在上面的基础上增加：

  ```
  methodology: string[], dataSources: SourceLink[], dataPoints: string[],
  underwritingRelevance: string,
  relations: { keyInsights: [], risks: [], followUps: [] }
  ```

路径里的 `categories/{categories_id}` 可以去掉：一个指标只属于一个类别。

### A3. Loan Portfolio 的返回结构未定义（"Follow API end"）
**建议**：

```
{ periods: [currentPeriodEnd, priorYearEnd],
  segments: [{ name, parentSegment, values: {<period>: amount},
               pctOfBook, nonaccrual, nonaccrualRatio }],
  methodology: string[], dataSources: SourceLink[] }
```

每个类别还可以带 `quality`（按不良率分档：< 0.50% Strong，0.50%–1.50% Satisfactory，
1.50%–3.00% Watch，≥ 3.00% Weak，没有数字为 Not reported）和 `qualityTrend`（与上一年末比，
升降 0.25 个百分点以上为 Deteriorating / Improving，否则 Stable）；整个贷款组合的不良率、质量和
覆盖比例见 `udf_GetCompanyLoanQualitySummary`。分档是提议，需要承保团队确认。

### A4. 趋势图拿不到趋势列表
接口是 `trends/{trend_name_id}`，但前端不知道有哪些趋势 id。

**建议**：改成 `GET api/companies/{id}/trends`，一次返回全部：
`[{ id, title, methodology, data: [{ year, value }] }]`。非银行返回空数组。

### A5. 银行专属模块的显示规则
Loan Portfolio 和 Performance Trends 只对银行有数据。

**建议**：`CompanyDetail` 加 `modules: { loanPortfolio: boolean, performanceTrends: boolean }`，
为 false 时前端隐藏该模块。

## B. 字段对齐

### B1. 来源只给一个，但数据有多个
`KeyInsight`、`RiskInsightRecord` 现在是 `source: string; link: URL`，表里每条都存了多个来源
`[{title, url, date}]`。

**建议**：统一用 `sources: SourceLink[]`；`link` 可保留为第一个来源，兼容现有设计。

### B2. `SourceLink` 的结构
前端定义是 `{source, link}`，表里是 `{title, url, date}`。

**建议**：改为 `{ title, url, date }`。财报电话会没有公开链接，`url` 和 `date` 为 `null`，此时只显示
标题。

### B3. Risk 和 Follow-up 缺 `relations`
弹窗的 Related 标签页需要它，数据在 `ItemLink`。

**建议**：`RiskInsightRecord` 和 `FollowUpRecord` 都加 `relations`，结构同 `KeyInsight`（相关的
keyInsights / risks / metrics / followUps 的 id 列表）。

### B4. `FollowUpRecord` 字段对应

| 前端字段 | 表 `FollowUpQuestions` | 建议 |
|---|---|---|
| title | title（问题本身） | 不变 |
| subtitle | summary（为什么要问） | 确认用 summary |
| type | category | 不变 |
| risk | followingRisk | 不变 |
| evidence | 表里没有，最接近的是 underwritingRelevance | 改名为 `underwriting`，或确认用这一列 |
| — | Priority | 增加 `priority`，或由 BFF 按它排好序 |

### B5. `RiskInsightRecord` 字段对应
`type` = 风险类型名；`category` = Governance & Leadership / Risk & Legal / Event Signals /
Behavioral Insights 四组之一；`underwriting` = UnderwritingRelevance；`evidence` =
SupportingEvidence。

**建议**：页面按这四组的顺序分组显示，BFF 返回时按此排序。

### B6. `industry` 显示什么
目前能给的是 SIC 描述，例如 "National Commercial Banks"。管线内部用 39 个行业组（例如
"Banks"），但这个名称还没写进应用表。

**确认**：显示哪一个。若要行业组名称，管线需在 `Company` 加一列（改表，要同步 DDL）。

### B7. `logo`
管线不提供。**确认**：由谁维护（应用自己存 URL 或 base64）。

### B8. `riskFlags`、`followUpQuestions` 计数和 `updatedAt` 的定义
**建议**：

- `riskFlags` = 当前（`isActive = 1`）Key Insights 中类型为 Warning 或 Negative 的条数；
- `followUpQuestions` = 当前问题数；
- `updatedAt` = `Company.UpdatedAt`，即管线最后一次更新该公司的时间。

### B9. `Activity` 可补字段（可选）
**建议**：加 `riskScore`（每条新闻的风险评分，来自 `NewsRiskScore`），以及时间范围参数，
例如 `?days=30`。

## C. 行为约定

### C1. 内容版本化，id 会变
Key Insights、Risks、Follow-ups、Key Takeaways 更新时会生成新行（新 id），旧行标记为非当前。

**约定**：

- 用旧 id 请求时返回 404，前端收到后重新拉列表。
- 用户对旧问题的 flag 不会自动转到新问题上，也不应该转（问题内容已经变了）。

### C2. 指标选择按公司还是按行业
清单里 `PUT .../metrics/selections` 挂在公司路径下，BFF 的用户表也按公司存；团队原数据模型按
行业存（用户在一个行业里选一次，同行业公司通用）。

**确认**：按哪种。

**建议**：

- 用户没选过时，用行业默认（`SIC_Preferences`），由 `metricSelections` 返回。
- `PUT` 的语义定为“替换该类别下的全部选择”。

### C3. 数值和期间的格式
- `USD` 是美元原值（不是百万）；`fraction` 是比例（0.049 显示 4.9%）；`ratio` 是倍数（0.53x）。
- `status = "NM"` 时 `value` 为 null，显示 "N/M"。
- 期间按公司自己的财年：`"FY2026 Q2"`、`"FY2025"`。
- `sourceLabel` 为 "Preliminary (earnings release)" 的数值必须标注出来（季报出来后会被替换），
  其他标签可放在提示里。

### C4. 两个导出接口的内容
`followUps/export` 和 `preferences/export`。

**确认**：文件格式（CSV / Excel / PDF）和包含的字段。数据都在现有表里，不需要新表。

### C5. 已有数据、清单里没用的（可选）
- 财报电话会的语气分析和风险标记（`TranscriptToneAnalysis`、`TranscriptRiskFlag`）。
- 如果要做，建议加 `GET api/companies/{id}/transcripts`。
