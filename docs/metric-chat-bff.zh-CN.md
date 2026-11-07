# uw_metric_chat {VERSION}：指标问答（给 BFF 团队）

来源：prof360 管线，提交 `{COMMIT}`。背景：Phase 19，D-088、D-090、D-091。

用户在指标弹窗里提问（"这个数怎么算的？""为什么是 N/M？"），BFF 调 SecureGPT 回答。回答只基于这家公司这个指标的数据，里面的每个数字都要对得上数据库里的数据点，对不上就不显示，改为显示固定的 "can't confirm"。

## 1. 放进 BFF

- 把 `uw_metric_chat/` 整个文件夹复制进 BFF 代码，位置随意，包内只用相对导入。**不要修改里面的文件**；升级时整个替换，并用 `CHECKSUMS` 核对（`shasum -a 256 -c CHECKSUMS`）。
- 依赖：`pydantic>=2.6`、`httpx>=0.27`，Python 3.11 及以上。不需要数据库驱动，也不引用管线的代码。
- 环境变量（与管线同名）：`SECUREGPT_CLIENT_ID`、`SECUREGPT_CLIENT_SECRET`、`SECUREGPT_SUBSCRIPTION_KEY`、`SECUREGPT_ENV`（dv / pp / pr）。凭证不会写进日志或错误信息。
- 数据库：先执行同目录下的 `bff_metric_chat.sql`（可重复执行），建表 `MetricChatContext`、`ChatPrompt`（管线写），`UserMetricChat`、`MetricIssueReport`（BFF 写），以及函数 `udf_GetMetricChatContext`、`udf_GetActiveChatPrompt`。

## 2. 接口

### `POST api/companies/{id}/metrics/{metricId}/chat`

请求：`{"conversationId": "<uuid，新会话时为空>", "message": "Why is Q4 2025 N/M?"}`

响应：

```json
{
  "conversationId": "…",
  "turnId": 123,
  "answer": "Interest coverage for FY2025 Q4 is N/M because EBIT was a loss of $3.65B …",
  "citations": [{"id": "dp:2025Q4:ebit", "label": "EBIT FY2025 Q4", "value": -3646000000.0,
                 "unit": "USD", "status": "OK", "period": "2025Q4"}],
  "possibleIssue": false,
  "issueSummary": null,
  "period": "2025Q4"
}
```

`citations[].period` 与弹窗的期间对应（`FY2025` = 财年，`2026Q3` = FY2026 Q3），前端点击引用可定位到那一期。

### `POST api/companies/{id}/metrics/{metricId}/chat/{turnId}/report`

用户点 "This looks wrong"：请求 `{"comment": "…", "period": "FY2025"}`，写一行 `MetricIssueReport`（`Source = 'USER'`）。管线定期读取，复核后修正数据；结论记在管线那边，不写回这张表。

## 3. 示例代码（FastAPI）

```python
from uw_metric_chat import (
    AsyncSecureGPT,
    ChatPromptRow,
    HistoryTurn,
    IncompatibleError,
    SecureGPTSettings,
    answer_question,
)

client: AsyncSecureGPT | None = None


@app.on_event("startup")
async def startup() -> None:
    global client
    client = AsyncSecureGPT(SecureGPTSettings.from_env())  # 每个进程一个，可共用


@app.on_event("shutdown")
async def shutdown() -> None:
    await client.aclose()


@app.post("/api/companies/{company_id}/metrics/{metric_id}/chat")
async def chat(company_id: int, metric_id: int, body: ChatRequest, user=Depends(current_user)):
    ctx_row = db.one(
        "SELECT * FROM dbo_professional360.udf_GetMetricChatContext(?, ?)", company_id, metric_id
    )
    if ctx_row is None:
        raise HTTPException(404, "No chat for this metric")
    prompt = ChatPromptRow.model_validate(
        db.one("SELECT * FROM dbo_professional360.udf_GetActiveChatPrompt(N'metric_chat')")
    )
    conversation = body.conversationId or uuid4()
    history = [
        HistoryTurn(question=r.Question, answer=r.Answer)
        for r in db.all(
            "SELECT Question, Answer FROM dbo_professional360.UserMetricChat "
            "WHERE ConversationId = ? AND UserId = ? ORDER BY TurnId",
            conversation,
            user.id,
        )
    ]
    try:
        reply = await answer_question(client, prompt, ctx_row.context, history, body.message)
    except IncompatibleError:
        # 数据库里的提示词或上下文比这个包新：需要换新文件夹（联系管线团队）
        raise HTTPException(503, "Chat is being updated")
    turn_id = db.insert(
        "UserMetricChat",
        {
            "ConversationId": conversation,
            "UserId": user.id,
            "CompanyId": company_id,
            "metric_id": metric_id,
            "Question": body.message[:2000],
            "ContextUpdatedAt": ctx_row.updatedAt,
            **reply.log_row(),
        },
    )
    if reply.possible_issue:
        db.insert(
            "MetricIssueReport",
            {
                "UserId": user.id,
                "CompanyId": company_id,
                "metric_id": metric_id,
                "TurnId": turn_id,
                "Source": "MODEL",
                "Period": reply.period,
                "IssueSummary": reply.issue_summary,
            },
        )
    return {
        "conversationId": conversation,
        "turnId": turn_id,
        "answer": reply.answer,
        "citations": [c.model_dump() for c in reply.citations],
        "possibleIssue": reply.possible_issue,
        "issueSummary": reply.issue_summary,
        "period": reply.period,
    }
```

## 4. 说明

- `answer_question` 不会因为模型或网关的问题抛异常：此时 `reply.grounding = "error"`，`reply.answer` 是固定的 "not available right now"，原因在 `grounding_detail`（写进 `UserMetricChat.GroundingDetail`）。只有包读不了数据库里的提示词或上下文时才抛 `IncompatibleError`。
- `grounding` 取值：`ok`（显示回答）、`rejected`（回答里有对不上数据的数字，显示 "can't confirm"，模型原文在 `RawAnswer`）、`error`。
- 默认模型 `gpt-6-luna-2026-09-22`；`answer_question(..., model="gpt-6-sol-2026-09-22")` 可切换。默认值以离线评估结果为准。
- 只用英文。问题最长 1,000 字符，会话只带最近 6 轮。
- 建议按用户限流（例如每小时 30 个问题）；单次调用超时 60 秒，网关重试 3 次。
- `UserMetricChat` 的保留期限待合规确认（暂按 365 天）。

有问题请联系管线团队（prof360）。
