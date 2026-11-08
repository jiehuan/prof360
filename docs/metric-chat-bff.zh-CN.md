# uw_metric_chat {VERSION}：指标问答（给 BFF 团队）

来源：prof360 管线，提交 `{COMMIT}`。背景：Phase 19，D-088、D-090、D-091。

用户在指标弹窗里提问（"这个数怎么算的？""为什么是 N/M？"），BFF 调 SecureGPT 回答。回答只基于这家公司这个指标的数据，里面的每个数字都要对得上数据库里的数据点，对不上就不显示，改为显示固定的 "can't confirm"。

## 1. 放进 BFF

- 把 `uw_metric_chat/` 整个文件夹复制进 BFF 代码，位置随意，包内只用相对导入。**不要修改里面的文件**；升级时整个替换，并用 `CHECKSUMS` 核对（`shasum -a 256 -c CHECKSUMS`）。
- 依赖：`pydantic>=2.6`、`httpx>=0.27`，Python 3.11 及以上。不需要数据库驱动，也不引用管线的代码。
- 环境变量（与管线同名）：`SECUREGPT_CLIENT_ID`、`SECUREGPT_CLIENT_SECRET`、`SECUREGPT_SUBSCRIPTION_KEY`、`SECUREGPT_ENV`（dv / pp / pr）。凭证不会写进日志或错误信息。
- 数据库：先执行同目录下的 `bff_metric_chat.sql`（可重复执行），建表 `MetricChatContext`、`ChatPrompt`（管线写），`UserMetricChat`、`MetricIssueReport`（BFF 写），以及函数 `udf_GetMetricChatContext`、`udf_GetActiveChatPrompt`。

## 2. 接口和前端

三个接口（建议问题、提问、报告）的请求、响应、错误码，以及前端的交互和文案，见同目录的 `INTERFACE.zh-CN.md`。

## 3. 参考实现（`reference_bff/`）

一份可运行的 FastAPI 实现，供参考，可以按 BFF 自己的结构改写（这一部分不是"勿改"的包）。它按顶层包导入 `uw_metric_chat`（与 `reference_bff` 放在同一层）；包放在别处时，改这几行 import 即可：

| 文件 | 内容 |
|---|---|
| `api.py` | `router`：三个接口；按用户限流（默认每小时 30 个问题，进程内计数，多个 pod 时换成共享存储）；`current_user` 依赖要换成 BFF 自己的鉴权 |
| `store.py` | `ChatStore` 接口和 `SqlChatStore`：用 SQLAlchemy 调 `udf_GetMetricChatContext`、`udf_GetActiveChatPrompt`，读写 `UserMetricChat`、`MetricIssueReport`（SQL 在 SQL Server 2022 上实测过）。BFF 用自己的数据访问层时，照着这里的 SQL 写即可 |
| `main.py` | `create_app()`：启动时建一个 `AsyncSecureGPT`（每个进程一个，关闭时释放）、存储层和设置，挂上 `router` |

接入现有 app 的最小改动：

```python
from reference_bff.api import ChatSettings, RateLimiter, current_user, router
from reference_bff.store import SqlChatStore
from uw_metric_chat import AsyncSecureGPT, SecureGPTSettings

app.include_router(router)
app.dependency_overrides[current_user] = my_current_user  # 返回带 .id 的用户


@app.on_event("startup")
async def chat_startup() -> None:
    app.state.chat_settings = ChatSettings()  # 模型、历史轮数、限流
    app.state.chat_limiter = RateLimiter(app.state.chat_settings.questions_per_hour)
    app.state.chat_store = SqlChatStore(engine)  # BFF 已有的应用库 engine
    app.state.chat_client = AsyncSecureGPT(SecureGPTSettings.from_env())


@app.on_event("shutdown")
async def chat_shutdown() -> None:
    await app.state.chat_client.aclose()
```

## 4. 说明

- `answer_question` 不会因为模型或网关的问题抛异常：此时 `reply.grounding = "error"`，`reply.answer` 是固定的 "not available right now"，原因在 `grounding_detail`（写进 `UserMetricChat.GroundingDetail`）。只有包读不了数据库里的提示词或上下文时才抛 `IncompatibleError`。
- `grounding` 取值：`ok`（显示回答）、`rejected`（回答里有对不上数据的数字，显示 "can't confirm"，模型原文在 `RawAnswer`）、`error`。
- 默认模型 `gpt-6-luna-2026-09-22`；`answer_question(..., model="gpt-6-sol-2026-09-22")` 可切换。默认值以离线评估结果为准。
- 只用英文。问题最长 1,000 字符，会话只带最近 6 轮。
- 建议按用户限流（例如每小时 30 个问题）；单次调用超时 60 秒，网关重试 3 次。
- `UserMetricChat` 的保留期限待合规确认（暂按 365 天）。

有问题请联系管线团队（prof360）。
