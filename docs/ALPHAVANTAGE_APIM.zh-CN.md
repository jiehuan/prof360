# Alpha Vantage：direct / APIM 配置

2026-11-03，D-075。默认 `direct` 保持官方接口调用；设置 `ALPHAVANTAGE_MODE=apim`
后改走公司 APIM，认证失败不会自动切回 direct。APIM 必须透传 Alpha Vantage 的查询参数
并返回兼容的原始 JSON；market、news、transcripts 的解析和公开来源链接不变。

## 1. 凭证与环境变量

两种模式都要求 `ALPHAVANTAGE_API_KEY`，均通过查询参数 `apikey` 发送。
APIM 模式还必须提供独立的 client ID、client secret 和 subscription key。

你维护的 Helm values 可以按下面的键组织；这只是 values 示例，实际 chart 还须把这些值
渲染到 Secret 并通过 `envFrom.secretRef` 或 `env.valueFrom.secretKeyRef` 注入容器。
仓库的原生 Kubernetes 参考文件采用 `Secret.stringData` + `envFrom.secretRef`。

```yaml
secrets:
  ALPHAVANTAGE_API_KEY: "<正式 AV API key>"
  ALPHAVANTAGE_CLIENT_ID: "<OAuth client ID>"
  ALPHAVANTAGE_CLIENT_SECRET: "<OAuth client secret>"
  ALPHAVANTAGE_SUBSCRIPTION_KEY: "<APIM subscription key>"
```

Client ID 通常不是秘密，但和凭证一起管理。真实 values/Secret 不提交 Git，不写进镜像、
普通 ConfigMap 或日志。不要复用 `SECUREGPT_*` 变量名；两套服务分别配置。

普通环境变量：

```dotenv
ALPHAVANTAGE_MODE=apim
ALPHAVANTAGE_ENV=runtime
ALPHAVANTAGE_CALLS_PER_MINUTE=5
ALPHAVANTAGE_DAILY_QUOTA=25
```

`MODE` 只允许 `direct` / `apim`，默认 direct；`ENV` 未设置则使用 YAML 的 `default_env`。
分钟速率必须为正，每日配额非负，0 表示不限；仍由环境变量唯一控制，按真实套餐填写。
direct 不要求三个 APIM 凭证，也不使用 APIM 环境选择。

## 2. 非敏感配置

将以下内容作为 `alphavantage.yaml` 挂到 `CONFIG_DIR` 下，示意地址须替换：

```yaml
direct:
  query_url: https://www.alphavantage.co/query
apim:
  default_env: runtime
  environments:
    runtime:
      query_url: https://av-gateway.example.invalid/alphavantage/query
      oauth:
        token_url: https://identity.example.invalid/oauth/token
        scope: REPLACE_WITH_AV_SCOPE
        token_auth_method: client_secret_post
        subscription_key_on_token: true
timeout_sec: 60
max_attempts: 3
```

`query_url` 是完整查询地址，程序不会再追加 `/query`。端点要求 HTTPS，地址不能内嵌
用户名、密码、查询参数或 fragment；`function`、`symbol`、`apikey` 等由客户端附加。
可在 `environments` 中定义 dv/pp/pr/runtime 等名称，每个环境的地址、scope 和认证方式独立。
选中环境残留 `REPLACE_WITH_*` 或 `.invalid` 占位值时拒绝运行。

旧配置挂载没有此文件时，direct 使用原有官方地址、60 秒超时和三次尝试；APIM 会报告
缺少环境。新增 YAML 的未知字段、非法认证方式、重复键和无效数值都会被拒绝。

## 3. token 接口格式

所有格式均为 POST，表单包含 `grant_type=client_credentials`，按接口约定选择：

| `token_auth_method` | client ID / secret | scope |
|---|---|---|
| `client_secret_post`（默认） | 表单 `client_id`、`client_secret` | 表单 `scope` |
| `client_secret_basic` | HTTP Basic；两个凭证各自先做表单编码 | 表单 `scope` |
| `gateway_headers` | 请求头 `Client_Id`、`Client_Secret`，沿用 SecureGPT 字段约定 | 请求头 `scope` |

`subscription_key_on_token: true` 表示获取 token 也携带 `Ocp-Apim-Subscription-Key`，
默认与现有公司网关约定一致；若身份服务不应收到这个 key，显式设为 false。
该选项**不影响业务请求**，APIM 业务请求始终携带 subscription key。
实际联调前需按网关文档/请求样例确认这两项；代码不会轮流尝试不同凭证格式。

token 响应必须包含非空字符串 `access_token` 和有效正数 `expires_in`（秒）。
`token_type` 可省略，提供时必须是 Bearer（大小写不敏感）。不使用 refresh token。

业务请求相当于：

```http
GET <APIM_QUERY_URL>?function=EARNINGS&symbol=ENPH&apikey=<AV_API_KEY>
Authorization: Bearer <ACCESS_TOKEN>
Ocp-Apim-Subscription-Key: <SUBSCRIPTION_KEY>
```

## 4. 运行、缓存与错误

- 仅在创建 AV 客户端时检查凭证；SEC-only 运行不需要 AV 凭证。缺失配置会成为相应
  AV 阶段的失败结果，保留现有编排和运行跟踪。
- 原始缓存命中时直接返回，不获取 token、不消耗调用配额。切换路径继续复用相同缓存键，
  因此网关必须保证数据语义一致；需要重新请求时使用原有刷新机制。
- token 只缓存在进程内，提前有效期的 10%（最多 120 秒）重新获取，短有效期不会被
  强行延长；过期判断保守地从 token 请求发出时计算。多线程共享同一个 token 缓存。
- APIM 业务请求遇到 401，最多强制重新获取 token 并重放一次；第二个 401、403 等直接失败。
  direct 的 401 不申请 token。重定向不会被自动跟随。
- token 和业务请求各自对网络故障、429 和 5xx 做有界指数退避重试；`max_attempts`
  包含首次尝试。401 重放前后的业务尝试各自受这个上限约束。
- token 请求不计入 AV 数据配额；每次业务请求尝试都计数，包括 401、临时错误和网络失败，
  因为客户端无法确定失败请求是否已被上游接收。token 失败时不计数据调用。
- 错误只保留安全状态/类别，AV 消息中的已知凭证会脱敏；回显真实长凭证的成功响应也拒绝保存。
  内部 APIM 地址、token、secret、subscription key 不写入公开来源链接或原始文件。

## 5. 验证与部署

```sh
PYTHONPATH=src .venv/bin/python -m uw_pipeline config check
PYTHONPATH=src .venv/bin/pytest -q tests/test_oauth.py tests/test_alphavantage_apim.py tests/test_alphavantage.py tests/test_alphavantage_deployment.py
```

`config check` 只检查配置结构，不要求真实凭证、不联系网关；AV 客户端创建时另行校验选中模式
的凭证与占位值。测试 token 为 `tests/fixtures/oauth/` 中的合成协议样例，数据使用已有 AV
fixture；未使用真实 OAuth 凭证或访问公司 APIM。本次不改动已有 `.env`。

已有 `deploy/openshift/secret.example.yaml` 增加了三个 APIM 凭证键，并保持 direct 默认值。
在你维护的现有 manifest 中注入这些环境变量，挂载相应 `alphavantage.yaml`，再将 MODE 设为
apim、ENV 设为你定义的环境名。若整目录挂载 `CONFIG_DIR`，需保留其他已有配置文件。
此变更不新增 CronJob 或修改调度。正式使用前在内网确认端点/认证方式，再用已有
`run --job` 做小范围联调。
官方 `scripts/alphavantage_demo.py` 仍显式使用 direct 和公共 demo key，不读取 APIM 凭证。
