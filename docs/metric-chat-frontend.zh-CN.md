# 指标问答：接口和前端说明（Phase 19.D，D-093）

读者：BFF 团队和前端团队。BFF 的参考实现（Python FastAPI）在 `reference_bff/`（仓库里是 `examples/bff_metric_chat/`），可以直接照着接；包本身的用法见 `README.zh-CN.md`。

## 1. 上线顺序

1. **数据库**（Databricks `run_sql_script`）：`pipeline_schema_full.sql`、`app_schema_changes.sql`、`bff_metric_chat.sql`，三个都可以重复执行。
2. **管线跑一次 nightly**：生成每家公司每个指标的上下文（`MetricChatContext`），写入提示词（`ChatPrompt`）。之后每次运行自动更新。
3. **BFF**：复制 `uw_metric_chat/` 文件夹，加上三个接口（见第 2 节），配置 SecureGPT 环境变量。管线的应用库账号要能 SELECT `MetricIssueReport`、`UserMetricChat`（管线每晚读取用户报告）。
4. **前端**：指标弹窗里的问答抽屉（见第 3 节）。

## 2. 接口

公共约定：路径里的 `companyId`、`metricId` 与指标弹窗相同（`Company.CompanyId`、`metrics.metric_id`）；字段用 camelCase；需要登录（BFF 现有鉴权）。

### 2.1 `GET api/companies/{companyId}/metrics/{metricId}/chat/suggestions`

打开抽屉时调用，返回三个预设问题；第二个问题在弹窗里有 N/M 的期间时会点名那一期。

```json
{"questions": ["How is Interest Coverage calculated?",
               "Why is Interest Coverage N/M for FY2025 Q4?",
               "Where does this number come from?"]}
```

`404`：这个指标没有问答（该公司不显示它，或还没有数据）：前端不显示问答入口。

### 2.2 `POST api/companies/{companyId}/metrics/{metricId}/chat`

请求：

```json
{"conversationId": null, "message": "Why is FY2025 Q4 N/M?"}
```

- `conversationId`：第一次为 `null`；之后带上一次响应里的值，BFF 会把同一会话的最近 6 轮一起发给模型。关闭抽屉或换指标时重新开始（传 `null`）。
- `message`：1–1,000 个字符。

响应 `200`：

```json
{
  "conversationId": "8f053f4d-…",
  "turnId": 123,
  "status": "answered",
  "answer": "Interest coverage for FY2025 Q4 is N/M because EBIT was a loss of $3.65B …",
  "citations": [
    {"id": "dp:2025Q4:ebit", "label": "EBIT FY2025 Q4", "value": -3646000000.0,
     "unit": "USD", "status": "OK", "period": "2025Q4"}
  ],
  "possibleIssue": false,
  "issueSummary": null,
  "period": "2025Q4"
}
```

| 字段 | 说明 |
|---|---|
| `status` | `answered`：正常回答；`out_of_scope`：问题超出这个指标（回答是一句说明）；`cannot_confirm`：回答没通过数字校验，`answer` 是固定文案；`unavailable`：模型暂时不可用，`answer` 是固定文案，可以让用户稍后重试 |
| `citations` | 回答用到的数据点。`value` 是原值（金额单位为元，`fraction` 的 0.049 = 4.9%，`ratio` 的 4.0 = 4.0x）；`period` 是弹窗里的期间：`FY2025` = 财年，`2026Q3` = FY2026 Q3 |
| `possibleIssue` / `issueSummary` | 模型认为这个值可能有问题时为 `true`，附一句说明（BFF 已自动记一条报告，`Source = MODEL`） |
| `period` | 回答主要说的期间，可用来高亮 |
| `turnId` | 这一轮的 id，报告时用 |

错误：`404` 没有问答上下文；`422` 请求不合法（空问题、过长）；`429` 提问太频繁（默认每人每小时 30 个，响应头 `Retry-After` 是秒数）；`503` 问答正在升级或还没配置好（管线和 BFF 版本不一致，需要换新的 `uw_metric_chat` 文件夹）。

### 2.3 `POST api/companies/{companyId}/metrics/{metricId}/chat/{turnId}/report`

用户点 "This looks wrong"。请求：

```json
{"comment": "Should include GM Financial?", "period": "FY2025"}
```

- `comment`：可选，最多 2,000 字符。
- `period`：可选，`FY2025` 或 `2026Q3` 格式；不填也可以。

响应 `201`：`{"reportId": 901}`。`404`：这一轮不存在或不是当前用户的。报告每晚进入管线的复核队列，复核结论不会写回 BFF。

## 3. 前端

**入口**：指标弹窗右上角加 "Ask about this metric"；打开时调 `suggestions`，`404` 时不显示入口。

**抽屉**：

- 顶部：指标名和公司名；一行提示 "Answers use only the data behind this metric."
- 三个预设问题按钮（来自 `suggestions`），点击即发送；下方输入框，Enter 发送，最多 1,000 字符。
- 发送后显示加载状态（通常 2–10 秒）；同一时间只允许一个问题在进行中。
- 回答下方显示引用标签：`label` + 格式化后的 `value`（如 "EBIT FY2025 Q4 · −$3.65B"）。点击标签：弹窗切到对应的视图（季度或年度）并高亮 `period` 那一期。
- `possibleIssue = true` 时，回答上方显示提示条：⚠ + `issueSummary`。
- 每个回答下方一个 "This looks wrong" 链接：弹出一个可选的文本框（"What looks wrong? (optional)"）和提交按钮，提交后显示 "Thanks — we'll review this value."。
- `status = cannot_confirm`、`unavailable` 时直接显示 `answer` 的固定文案（灰色），不显示引用；`unavailable` 时显示 "Try again" 按钮。
- `429`：显示 "You've asked a lot of questions this hour. Please try again later."；`503`：显示 "The assistant is being updated. Please try again later."，并隐藏输入框。
- 关闭抽屉、切换指标或公司时清空会话（下次 `conversationId` 传 `null`）。

**界面文案**（英文，可直接用）：

| 位置 | 文案 |
|---|---|
| 入口按钮 | Ask about this metric |
| 抽屉提示 | Answers use only the data behind this metric. |
| 输入框占位 | Ask how this value was calculated, where it comes from, or why it changed |
| 报告链接 | This looks wrong |
| 报告文本框 | What looks wrong? (optional) |
| 报告成功 | Thanks — we'll review this value. |
| 限流 | You've asked a lot of questions this hour. Please try again later. |
| 升级中 | The assistant is being updated. Please try again later. |

## 4. 记录和隐私

- 每一轮问答都写 `UserMetricChat`（问题、展示给用户的回答、模型原文、引用、校验结果、模型、提示词版本、token、耗时）。保留期限待合规确认（暂按 365 天）。
- 管线只读用户报告和对应那一轮问答，不读其他问答；不保存 UserId（只存哈希，用于统计不同用户数）。
- 只用英文。
