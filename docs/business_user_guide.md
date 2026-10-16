# Project Use Guide — prof360

本页是使用导航。日常操作正文仍见 [OPERATIONS.md](OPERATIONS.md)，对外表约定正文仍见
[schema-contract.md](schema-contract.md) 的应用库部分。这里补充入口、结果解读和容易误用的边界，
不复制整份运维或接口文档。范围规则见 [AGENTS.md](../AGENTS.md#authority-map)。

<!-- capability-anchor: DOC.business_user_guide -->

## 0. Documentation and Interface Entrypoints

| 入口 | 用途与正式参考 |
|---|---|
| `uw-pipeline --help`、`uw-pipeline run --help` | CLI 是本仓库运行接口；定义在 `src/uw_pipeline/cli.py`，安装注册见 `pyproject.toml`。运维用法见 OPERATIONS §1。 |
| `run` | 按公司/阶段或 `--job` 执行；阶段配置见 `config/jobs.yaml`。单独 publish 同样需要公司选择。 |
| `runs`、`coverage-report`、`dq-report`、`audit-insights` | 运行状态、指标缺口、质量发现、抽样证据审计；报告默认写 `data/reports/`。 |
| `review`、`review approve/reject` | 有署名的人工复核；OPERATIONS §10b，命令实际参数以 CLI help 为准。 |
| `init-db` | 本地 SQLite 建表/补列；服务器库使用 OPERATIONS §10 和 `deploy/sql/`。混合配置的成功退出不证明应用库已初始化，分支见 interact §3。 |
| 应用数据库表 | `schema-contract.md` 应用库部分、`schema_app.py`；供外部 BFF 读取，重点核对当前版本、关联及 source 字段。 |
| 开发与维护 | [AGENTS.md](../AGENTS.md)、`TECHNICAL.md`、`decisions.md`、`PLAN.md`，以及 [TESTING.md](../TESTING.md)。计划不是当前实现证明。 |

HTTP API、MCP server 与 Web UI：**Not applicable — 本仓库的交付入口是 CLI 和应用表，
未包含这些服务实现**。外部 BFF 的 API 地址和部署接入资料：尚未确认，仓库内不足以核实。

## 1. Value

管线把公司申报、财报、电话会议和可用新闻整理为财务指标、风险线索、来源和跟进问题。
工程人员负责配置/运行，数据和承保使用者通过接入的应用消费结果。
有来源和经过程序校验能帮助复核，但不能把自动生成的说明当作最终业务判断。

## 2. Typical Uses

<!-- capability-anchor: CAPABILITY.configured_pipeline -->
<!-- capability-anchor: CAPABILITY.sourced_metrics -->

日常刷新使用既定 job；指标修订后按已准备的输入重算相关阶段；检查缺口与 DQ 后再核对来源。
新增公司/指标、修改 prompt、数据库部署分别沿用 OPERATIONS §2–6、§10。
输入、失败和组合语义见 [interact.md](../interact.md)，不要把一条示例命令理解为全套环境安装。

## 3. Capability and Responsibility Boundaries

<!-- capability-anchor: RESPONSIBILITY.bff_owned_state -->
<!-- capability-anchor: BOUNDARY.validation_scope -->

管线写应用表，BFF 管理 logo、收藏、最近访问及用户标记，实际 UI 验收需在外部应用完成。
运行成功不等于数据全新或无质量问题；发布成功不等于模型、数据库环境和 BFF 均已验收。
模型可能漏项或误解原文，即使引文、数字、表格位置和双模型核对通过，也需要业务复核。
待开发的扩围与并发能力按 PLAN 的状态理解，不作为本版本使用条件。

## 4. Context to Provide

运行前安装项目与所需依赖，按 `.env.example` 配置 `SEC_USER_AGENT`、目标数据库、原始存储和
所用外部服务的凭据。公司应已在 `config/companies.yaml` 中登记固定 CIK；不能只给任意股票代码
就期待系统自动接入。`--all` 选择活跃公司，`--ticker` 可重复；`--job` 与 `--stage` 不可组合。
未提供模型/AV 凭据时，应选择确实可运行的阶段并准备输入，不能期待完整任务退化为离线成功。
密钥只放环境变量或忽略的本地 `.env`，不要放进反馈、日志示例或 fixture。

在已准备好上述输入的环境中，`.venv/bin/uw-pipeline run --all` 选择全部活跃公司并按默认阶段
顺序运行；当前顺序见 architecture，不能从命令正常退出推断所有数据都已刷新。
`.venv/bin/uw-pipeline run --ticker MAR --force` 只把强制标志传给 `company`、`facts`、
`documents`，按这些阶段的实现跳过相应原始缓存或输入检查；下游阶段的输入哈希判断和
`cached_call` 的合格模型缓存仍有效，不能用它保证所有阶段重新计算。缓存和依赖限制见
interact §2、architecture。

## 5. Representative Real Cases

### 已采集 MAR 的数据后重算指标

```bash
uw-pipeline run --ticker MAR --stage compute
uw-pipeline coverage-report --ticker MAR
```

先确认公司和 facts 已入库。第一条输出任务摘要，第二条写覆盖率报告并列出缺少解释的指标。
缺口需回查原文件和 `coverage_notes.yaml`；测试依据是 `tests/test_phase2_golden.py`，
不能从固定 fixture 中的某个数字推断当前实时财务值。

### 将当前结果同步到应用库

```bash
uw-pipeline run --ticker MAR --stage publish
```

输入是当前管线数据库，输出是配置指向的应用库；`tests/test_publish.py` 覆盖映射、幂等和版本 ID。
这不会替你刷新上游或验证 BFF 页面。原运维文档中的简写 `run --stage publish` 需要补充公司选择。

### 复核争议信号

先运行 `uw-pipeline review` 查看证据和当前 ID，再执行 approve/reject 并提供真实署名和依据。
被拒绝信号不再进入后续洞察输入；已有结果需要后续重建和发布才反映变化。
依据为 D-045 和 `tests/test_governance.py`，不要把 approve 当成自动修复财务数值。

## 6. Reading Results

查看 CLI 退出码及摘要，并通过 `runs`、`dq-report`、`coverage-report` 分别核对任务、质量和覆盖。
DQ Error 本身不会让 dq 阶段失败；无缺口说明的报告也不是程序异常。
`dq-report` 未指定 `--run-id` 时要求找到成功的 DQ 任务；显式指定 ID 则直接查询发现，
不存在或未执行过 DQ 的运行也可能得到 `no findings` 报告。空结果只表示未查到发现，
需要结合运行和 DQ 任务状态判断是否真正做过检查。
PRELIMINARY 表示初步来源，NM 与缺失/零不同；外部展示应按正式表契约处理。
人工判断要核对公司、财年/期间、单位、来源和当前版本，不只看“有数据”。

## 7. Human Escalation

质量或恒等式问题先查原始文件并使用已有 review 路径；配置/凭证问题按 OPERATIONS 排查。
BFF 统计口径和字段差异沿用 `schema-contract.md` 的待对齐入口；超出既有政策的产品选择写入
`decisions.md`。具体未决公共语义见 [interact.md 的 Open questions](../interact.md#6-open-questions)。

## 8. Feedback and Ownership

仓库的开发、运维与接口文档定义了上述职责范围；未发现 CODEOWNERS、专门客服渠道或支持 SLA，
这些机制是 **Not configured**。个人值班人及外部团队当前分工尚未确认，不在本页猜测。
反馈至少带命令、run ID、公司/期间、可复现输入与去除秘密后的错误，便于维护者回到证据。
