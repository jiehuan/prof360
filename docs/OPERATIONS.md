# 运维手册

读者：维护这条管线的工程师。设计和背后的理由见 `docs/decisions.md`（D-xxx），开发约定见 `CLAUDE.md`。

## 1. 日常运行

| 命令 | 用途 |
|---|---|
| `uw-pipeline run --job nightly` | 定时任务。nightly 在美东 23:30 运行，morning 在 05:30 运行，阶段定义在 `config/jobs.yaml` |
| `uw-pipeline run --ticker MAR --stage compute` | 手动运行指定公司的指定阶段，`--stage` 可以写多次 |
| `uw-pipeline runs` | 列出最近的运行，以及最近一次里失败或被跳过的任务和原因。每次运行旁边显示数据质量结果：`dq={'Error': 1, 'Warning': 3}`；没跑 dq 阶段的显示 `dq=not run`。运行成功不代表质量通过：dq 查出 Error 时任务仍是 SUCCEEDED，详情看 `dq-report` |
| `uw-pipeline dq-report` | 生成数据质量报告，写到 `data/reports/dq-run<id>.md` |
| `uw-pipeline coverage-report` | 生成 XBRL 指标覆盖率报告，并列出没有说明原因的缺口 |
| `uw-pipeline run --stage publish` | 把当前结果写进应用库（nightly、morning、news 三个 job 末尾都会跑） |
| `uw-pipeline audit-insights` | 抽样检查 LLM 洞察：引用的原文能否找到、来源是否都有标题、追问问题是否都有来源、贷款行能否在原文找到、非 FI 公司是否混入了 FI 数据 |

**运行状态**：`pipeline_run.status` 有以下几种取值：

| 状态 | 含义 |
|---|---|
| SUCCEEDED | 全部成功 |
| PARTIAL | 部分任务失败或被跳过 |
| FAILED | 没有任何任务成功 |
| RUNNING | 正在运行，或者进程已经死掉 |

**续跑**：进程或 pod 被杀之后，再次启动同一个 job（阶段和公司都相同）会自动接着跑，前提是上一次运行的心跳已经超过 30 分钟，并且它开始不到 12 小时（`stale_after_minutes`、`resume_within_hours`，在 `config/jobs.yaml`）。更早的、或者阶段和公司不同的中断运行会被关闭（FAILED，`stats_json` 里有 `abandoned`），新运行从头开始。运行期间每 5 分钟刷新一次心跳（`heartbeat_minutes`），所以耗时很长的任务不会被误判为已死。

**运行锁**：只要有任何 job 正在运行（心跳仍然新鲜），新的启动就会以退出码 2 退出。不同的 job 也一样：例如 nightly 运行时到点的 news 会跳过这一轮，因为两者写同一批表。

**费用**：每次运行的 LLM token 用量和估算费用记录在 `pipeline_run.stats_json` 中，`runs` 命令也会显示。

## 2. 新增公司

1. 在 `config/companies.yaml` 中加一项，**必须固定 CIK**。CIK 可以在 `https://www.sec.gov/files/company_tickers.json` 查到，但要到 submissions 里核对公司名称：ticker 会被转给别的公司，例如 PARA 现在属于 Banzai。
2. 如果公司经历过合并、目前的申报主体是后继公司，设置 `history_start`，只使用后继主体的数据（参考 SKYD 的写法）。
3. 确认它的 SIC 在 `config/industry_groups.yaml` 里有对应的行业组。没有的话，见下文"新增行业组"。
4. 运行 `uw-pipeline run --ticker XXX`，然后运行 `coverage-report`。对每一个缺口，要么修正 tag 映射，要么在 `config/coverage_notes.yaml` 里写明原因。
5. 首次运行的 LLM 费用约为每家公司 $1.5–3。
6. MVP 阶段的约定是每个 SIC 只放一家公司（D-007）。

## 3. 新增指标或修改指标集合

- **计算类指标**：在 `config/metrics.yaml` 中定义 `formula`。所需的 XBRL 概念加到 `config/concept_map.yaml`：候选 tag 会**按期间逐个选择**，排在前面的优先；如果某个行业组需要不同的 tag，用 `by_group` 覆盖。
- **文本类指标**（`kind: text`）：写清 `definition`，包括口径和需要排除的情况；配好 `keywords`；银行的资本类指标设 `dual_check: true`，要求两个模型的结果一致。
- **加入行业组**：把指标放进某个 `metric_sets`，或者加到行业组的 `metrics` 列表。**指标清单只在配置中维护，不在运行时生成**（D-016）。
- **验证**：运行 `pytest`，然后对一家受影响的公司运行 `run --stage compute`（文本类指标运行 `--stage textmetrics`），最后看 `coverage-report`。

## 4. 新增行业组或 SIC

1. 在 `config/industry_groups.yaml` 的 `groups` 下新增一组，设置 `is_financial`（决定 FI 模块是否出数据）和 `metric_sets`。
2. 在 `sic:` 下加精确的 SIC 映射。范围映射只作兜底。
3. 保险业目前设为非 FI（D-008）。如果业务方要求改，修改 `is_financial` 即可。

## 5. 公司特有的设置

文件放在 `config/company_overrides/<TICKER>.yaml`：
- `concepts`：替换某个概念的候选 tag。可以写公司扩展 taxonomy 或带维度的 tag，管线会从每份申报的 XBRL instance 里读取，参考 `MAR.yaml` 中的 fee revenue。
- `metrics`：公司专属指标。
- `sections`：标准的 Item 标题切不出章节时，用起止标题的正则来定位，参考 `JPM.yaml` 中的 MD&A。

## 6. 修改 prompt 或 grounding 规则

1. **不要改已有的 prompt 文件**。新建 `src/uw_pipeline/enrich/prompts/<task>_v<N+1>.md`，然后修改 `config/llm.yaml`、`config/insights.yaml` 或 `config/text_extraction.yaml` 里的版本号。版本号会进入缓存键，所以改版后会重新调用 LLM。
2. 对 golden 公司重跑：`uw-pipeline run --ticker MAR --ticker JPM --ticker ENPH --stage insights`。
3. 导出结果：`python scripts/export_golden.py`。
4. 用 `git diff tests/golden` 审阅输出的变化，确认之后把 golden 文件一并提交。
5. 修改了 grounding 规则（`enrich/grounding.py` 或 `enrich/insights.py`）时，要在 `jobs/run_insights.py` 里提升 `GROUNDING_VERSION`。这样已缓存的 LLM 输出会按新规则重新校验，**不会产生 LLM 费用**。

## 7. 详情弹窗数据（Phase 8，D-036）

- `metricdetail` 阶段排在 `insights` 之后：Methodology 和 Data Sources 由规则生成；每家公司一次 Opus 调用写各指标的 Underwriting Relevance 和关联。输入不变时不调用 LLM。没有 LLM 时规则部分照常运行。
- 章节的可读名称、公式里概念的名称在 `config/source_labels.yaml`。新增章节代码或概念时在这里加一项，否则界面上会直接显示代码。
- 银行的 5 年历史由 `config/industry_groups.yaml` 的 `mdna_history_years` 控制，只保存旧 10-K 的 MD&A。
- 贷款组合的口径在 `config/text_extraction.yaml` 的 `loan_portfolio`。只保留两个模型一致、且原文给出余额的类别，不会从百分比反推余额。

## 8. 应用库、新闻和评分（Phase 9，D-037）

- `publish` 阶段把管线结果写进应用库（`APP_DATABASE_URL`，dev 是 `data/app.db`）。管线不写 `Company.logo` 和用户相关的表。重跑只写有变化的行。
- 应用库的映射规则（指标分类、风险类型、confidence）在 `config/app_publish.yaml`。如果 `RiskTypes` 里出现 "Other"，在 `risk_types` 里加一条规则即可，重新 publish 不需要 LLM。
- 新闻相关性：公司别名在 `config/companies.yaml` 的 `news_aliases`；排除规则和 LLM 判断在 `config/news.yaml`。新闻阶段的 `pruned` 是按当前规则删掉的旧文章数。
- 风险评分在 `scores` 阶段（`config/scores.yaml`）。只有重要新闻进入洞察输入，阈值在 `config/insights.yaml` 的 `news_materiality`；阈值调低会让洞察更常重新生成（每家公司每次约 $0.9）。
- 比较模型：`python scripts/compare_models.py --model claude-sonnet-5-5 MAR JPM ENPH`，报告在 `data/reports/`。

## 9. LLM：SecureGPT、Claude、Runware（D-039、D-047）

- `config/llm.yaml` 里每个提供方有一套完整的模型配置（抽取、复核、综合、价格）。用环境变量 `LLM_PROVIDER` 选择，不设时用 `default_provider`（securegpt）：
  - **securegpt**（公司网络内）：抽取 gpt-5-nano，复核和综合 gpt-5-mini。网关地址、api-version、部署名在 `config/securegpt.yaml`。
  - **anthropic**（公司网络外开发和测试）：抽取 Claude Haiku 5.5，复核和综合 Claude Sonnet 5.5，需要 `ANTHROPIC_API_KEY`。
  - **runware**（经 Runware 网关，OpenAI 兼容接口）：抽取 DeepSeek V4 Flash（`deepseek:v4@flash`），复核 Claude Haiku 5.5，综合 Claude Sonnet 5.5，需要 `RUNWARE_API_KEY`，账户余额至少 $5。网关设置在 `config/runware.yaml`；先跑 `python scripts/runware_smoke.py`（每个模型、每种输出方式调用一次，不写数据库）。新增模型时，smoke test 显示只支持 JSON 模式的，写进 `output:`。
- 每个提供方的缓存各自保留：切换回来时，输入没变的任务直接用缓存，不产生费用。
- 凭证只放环境变量：`SECUREGPT_CLIENT_ID`、`SECUREGPT_CLIENT_SECRET`、`SECUREGPT_SUBSCRIPTION_KEY`，环境用 `SECUREGPT_ENV`（dv / pp / pr）。
- 切换或更换环境前先跑 `python scripts/securegpt_smoke.py`：每个模型调用一次，并测试网关是否接受 `reasoning_effort`；接受的话把 `send_reasoning_effort` 改为 true。
- 模型输出没通过 schema 校验，或者输入超出模型的上下文长度时，只有这一项失败（记录在 `llm_call.error`），其他项照常运行；校验失败的会在下次运行时重新请求。
- 在公司网络外开发：本地 `.env` 里设 `LLM_PROVIDER=anthropic`。不要在两个提供方之间来回切换同一个部署环境：每次切换，当前版本的洞察会换成另一个提供方的结果。

## 10. Azure SQL 和 ADLS（D-040）

- 公司网络内：`DATABASE_BACKEND=azure_sql`、`STORAGE_BACKEND=adls`、`AZURE_ENV`（dv / pp / pr），凭证用 `AZURE_SQL_CLIENT_ID/SECRET` 和 `ADLS_CLIENT_ID/SECRET`。服务器、数据库、schema、存储账户、容器、路径前缀在 `config/azure.yaml`（目前是占位值，标了 TO EDIT）。
- 公司网络外：不设这些变量，照常用 SQLite 和 `data/raw`。
- 管线表和应用表在两个 schema（`pipeline_schema`、`app_schema`），**不能放在同一个 schema**：SQL Server 表名不区分大小写，应用的 `Company` 和管线的 `company` 会冲突。
- 建表：由工程师执行 `deploy/sql/pipeline_schema_full.sql`、`app_schema_full.sql`（新环境）或 `app_schema_changes.sql`（已有数据模型）。改了表结构或 schema 名后，运行 `python scripts/export_app_ddl.py` 重新生成。
- 首次运行前：`python scripts/azure_smoke.py`，检查连接、schema、表、权限和 ADLS 读写，不会改动任何业务表。ALTER 和 CREATE TABLE 只显示 WARN：只有自己建表才需要，日常运行只用临时表。
- `uw-pipeline init-db` 只用于本地 SQLite；在 Azure SQL 上它会提示改用 `deploy/sql/` 的脚本。
- 改了涉及数据库的代码后，在本机跑 `scripts/test_mssql.sh --all`：用本地 Docker 的 SQL Server 运行全部测试（不需要公司网络）。用完 `scripts/test_mssql.sh --stop`。

## 10a. 结构化数据优先和恒等式（Phase 13，D-042）

- 银行资本比率从 XBRL 取，候选在 `company_overrides/<TICKER>.yaml` 的 `concepts`（成员名各家银行不同）。新增银行时先在它的 10-K instance 里找 `RiskWeightedAssetsCalculationMethodologyAxis` 下标准法、集团合并的事实，再配置；没配置就继续用文本抽取。
- 恒等式在 `config/dq_rules.yaml` 的 `metric_identities`。DQ 报 `metric_identity` 时：Error 表示库里的值不满足（XBRL 来源也会报），Warning 表示文本抽取的值因不满足被丢弃，看 textmetrics 日志里的 `text values dropped`。
- 委托书（DEF 14A）的薪酬数据只进洞察输入，不在指标页显示。某公司没有数据时，先看它的 DEF 14A 是否带 `ecd` 标签；同一年两任 CEO 的年份不出数（不相加）。

## 10b. 读表、复核、预算（Phase 14，D-043–D-046）

- 表格类指标（RevPAR、贷款分类、AUM、VaR 等）由模型给出单元格坐标，程序核对后才采用；被拒绝的会在 textmetrics 日志里记 `withheld` 和原因。新公司版式不同时，先看这些原因，再调整 `metrics.yaml` 里该指标的 `table`（`column`、`scope`、`exclude_rows`）。
- 董事会构成：`governance` 阶段（在 textmetrics 之后）读最新的委托书（DEF 14A），两个模型一致且引文能在原文找到的才保存，否则为空。没有委托书的公司（例如 SKYD）显示 `no proxy statement`，属于正常。单独重跑：`run --ticker MAR --stage governance`，之后跑 `--stage signals` 更新董事会信号。
- 人工复核：`uw-pipeline review` 列出待复核的项；`uw-pipeline review approve|reject <id> --reviewer '姓名' [--note '理由']`。被驳回的信号不再进入洞察。
- 每日模型费用上限：`LLM_DAILY_BUDGET`（默认 25，按当前提供方的币种）。超过后新调用逐项失败，次日运行再补，日志有 `daily LLM budget`。
- 稳定性检查：`python scripts/stability_check.py --runs 3 MAR JPM`（真实调用，有费用）；`--faults` 检查超时、限流、无效输出是否安全失败（无费用）。记录在 `data/reports/stability-*.json`。结果里同一个值数字不同才是问题；某个值在一次运行里有、另一次没有，是校验没通过被扣下，属于覆盖率波动。
- 贷款分类：两个模型都要读出同一个数才保存。两个模型读的层级不同（一个给整个批发贷款一行，另一个给分类）时，只要分类加起来等于那一行，就保存那一行（JPM 显示为 Wholesale / Loans retained）。

## 11. 常见故障

| 现象 | 原因与处理 |
|---|---|
| transcripts 显示 `daily quota ... used` | 检查 `ALPHAVANTAGE_CALLS_PER_MINUTE=75`、`ALPHAVANTAGE_DAILY_QUOTA=0`（付费 key）是否已设置 |
| transcripts 显示 `unavailable` | AV 没有这家公司这一季的记录。改过 ticker 的公司，AV 常把旧数据留在旧代码下：在 `config/companies.yaml` 设置 `av_symbols`（新代码在前，例如 SKYD 是 `[SKYD, PSKY]`），下次运行会按新列表重试，新闻也会重新回补 30 天 |
| SEC 返回 403/429 | 检查 `SEC_USER_AGENT` 是否包含联系邮箱，限速不要超过 10 次/秒（默认 8）。客户端已经带了重试 |
| 日志里出现 `LLM unavailable` | 凭证、环境或部署名有问题（SecureGPT：检查 `SECUREGPT_*` 和 `config/securegpt.yaml`，先跑 smoke test）。此时 signals 会退回纯规则，textmetrics 和 insights 会失败 |
| 运行一直显示 RUNNING | 进程已经死了。30 分钟后再次启动同一个 job 就会续跑（12 小时之内）；也可以等下一次 CronJob。超过 12 小时的会被关闭，新运行从头开始 |
| 退出码 2 | 有运行正在进行（任何 job，运行锁），无需处理；被挡住的 news 会在下一个时间点运行 |
| DQ 报出 `quarters_vs_year` 错误 | 通常是公司在新申报里重述了前期数据，新旧口径混在一起（例如 LUV 2025 年的 capex，见 D-035）。要核对原始文件；等下一份 10-K 入库后，口径通常会恢复一致 |
| DQ 报出 `quarter_jump` | 先在申报文件里核实，常见原因是一次性的减值或计提。如果是单位错误，修正 concept 映射 |
| `coverage-report` 列出 UNEXPLAINED | 要么修正 tag 映射，要么在 `coverage_notes.yaml` 里写明原因 |
| DQ 报出 `detail_coverage` | 没跑 `metricdetail`，或模型输出的数字没通过校验被丢弃。重跑 `--stage metricdetail`；仍然缺失时看日志里的 `metric relevance dropped` |
| DQ 报出 `link_integrity` | 关联指向了已经被替换的洞察。重跑 `--stage insights --stage metricdetail`（输出来自缓存，不产生费用） |
| DQ 报出 `metric_identity`（Error） | 存下的值违反了指标关系（例如 CET1 > Tier 1）。核对原始文件；确认后用 `uw-pipeline review` 记下结论 |
| DQ 报出 `debt_current_portion`（Warning） | 公司没有标注一年内到期的债务，总债务只含长期部分（例如 Macy's）。属于数据缺口，不需要处理 |
| 日志里出现 `daily LLM budget` | 当天新调用超过 `LLM_DAILY_BUDGET`。未完成的项目第二天会自动补上；需要当天补完时临时调高该变量 |
| DQ 报出 `source_coverage`（Info） | 洞察只引用了电话会议记录，没有公开链接，属于预期情况 |

## 12. 密钥和费用

- 密钥只放在 `.env`（本地，已被 git 忽略）或 OpenShift Secret 中，**不要写进代码、日志或测试数据**。Alpha Vantage 的报错消息里会带 key，客户端已经做了脱敏。
- 提交前检查：用 `git grep --cached` 搜索 Anthropic key 的固定前缀和 Alpha Vantage key 的前几位，应该没有任何结果。
- LLM 调用会按输入哈希缓存，输入不变时不会再次调用。每晚的费用只来自内容发生变化的公司。可以用 `runs` 查看每次运行的估算费用。
