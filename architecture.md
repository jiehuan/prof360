# Architecture — prof360 / uw-pipeline

## 0. Scope and Update Triggers

本页说明本仓库的数据管线，覆盖 CLI、采集、计算、模型处理、两类数据库和发布边界。
Professional 360 的前端及 BFF（供前端调用的后端服务）不在本仓库；写入应用库不证明它们已完成对接。
既有设计详解见 [docs/TECHNICAL.md](docs/TECHNICAL.md)，政策来源见
[Authority Map](AGENTS.md#authority-map)。入口、阶段依赖、数据口径、持久化、配置或对外表结构变化时，
核对本页相应内容及末尾的传播关系。

## 1. System Purpose

`uw-pipeline` 将 SEC 申报资料和 Alpha Vantage 数据整理为公司信息、财务指标、规则信号、
有来源的洞察与追问，再写入 Professional 360 使用的应用表。公司与指标范围由 `config/` 配置。
它是批处理程序，不提供本仓库内的 Web UI、HTTP API 或 MCP 服务，也不执行最终承保决定。
公开使用入口统一见[用户指南](docs/business_user_guide.md#0-documentation-and-interface-entrypoints)。

## 2. Runtime Entrypoints and Main Flows

`pyproject.toml` 将 `uw-pipeline` 注册到 `uw_pipeline.cli:app`。
`cli.run` 解析公司和阶段，`Orchestrator.start/execute` 建立运行与任务，
`StageRunner._stage` 分发到 `jobs/run_*.py`，结果保存到数据库并汇总为 CLI 退出码。

```text
配置 + 环境变量 → CLI → Orchestrator → StageRunner → 各阶段
SEC / Alpha Vantage → clients → 原始存储 → extract / normalize
  → facts / compute / documents / transcripts / news
  → textmetrics / governance / signals / scores / insights / metricdetail
  → 管线数据库 → publish → 应用数据库 → 外部 BFF
                    └→ dq → 质量问题记录与报告
```

图中概括数据方向，实际执行顺序与依赖不是同一概念：

- 手动默认顺序由 `cli.STAGE_ORDER` 定义：company → facts → compute → documents →
  transcripts → news → scores → textmetrics → governance → signals → insights →
  metricdetail → publish → dq。
- `config/jobs.yaml` 的 nightly、morning 不含 news；news 作业只有 news、scores、publish。
- `jobs/orchestrator.py:DEPENDS` 只检查**同一次运行中已选入的前置阶段**。指定 `--stage`
  不会自动补齐前置阶段，也不会自动排序用户传入的阶段；单独重跑必须已有所需输入。
- publish 只依赖同次运行中的 compute，dq 也只依赖 compute。LLM 阶段失败不必阻止 publish；
  dq 在默认顺序中位于 publish 之后，且质量发现本身不使 dq 任务失败。

## 3. Architecture Invariants

| 约束及依据 | 适用边界、反例检查与违反后果 |
|---|---|
| 公司身份以配置固定的 CIK 为准，ticker 用于选择与展示；政策 D-006、开发约定 | `extract/company.py` 与 `test_pinned_cik_beats_reassigned_ticker` 覆盖 ticker 被转让时仍选原 CIK。底层模型允许未固定 CIK，但正式新增公司遵守固定 CIK 政策；否则可能读入另一主体。 |
| 指标集合、公式和行业映射由配置维护；D-016、开发约定 | `compute/registry.py`、`config.py` 装载并检查引用；不得把运行时猜测的指标集合当成项目政策。变更须核对行业、公司 override 和来源口径。 |
| 结构化事实优先；D-042，`run_text_metrics.py` | XBRL 已覆盖的期间（包括 NM）不被初步文本值替代；文本值用于交叉核对或填补缺口。`test_stage_writes_preliminary_and_crosschecks` 覆盖该边界。 |
| 仍存在的同一对象映射重跑保持身份；D-037 | `AppWriter` 在映射和应用行仍存在时按 ID 更新；KeyInsights、StrategicRisk、FollowUpQuestions 的新版本另建行并停用旧行，旧问题 ID 不改成另一问题。其他表可删过期行，外部删行后重建可产生新 ID。以 `tests/test_publish.py` 核对，不推导跨库原子性。 |
| 模型业务输出须经任务适用的校验；开发约定、D-032/D-044 | schema、引文、数字、表格坐标或双模型核对各有范围。`llm_call` 是缓存/审计表，可能先保存 schema 合格但尚未通过任务校验的回答；不能把缓存记录当作已采纳的业务结果。 |
| 本地自动化测试不调用外部业务 API；开发约定 | `tests/conftest.py:_no_network` 拦截 Python socket；HTTP 用 respx、模型用 FakeLLM。SQL Server 的原生 ODBC 路径另设集成测试，不由该 socket 拦截证明隔离。 |

## 4. Module Responsibility Boundaries

| 模块 | 职责及依赖边界 |
|---|---|
| `cli.py`、`jobs/orchestrator.py`、`jobs/stages.py` | 选择任务、记录状态、检查依赖、重试和组装客户接口；不定义财务公式。 |
| `clients/` | SEC/AV/模型网关、限速、数据库连接与本地/ADLS 原始存储；任务通过这些边界访问外部系统。 |
| `extract/`、`normalize/` | 公司/申报选择，HTML 与表格整理，财年期间和 XBRL 事实归一化。 |
| `compute/`、`quality/`、`signals/` | 指标注册与计算、覆盖率、恒等式/质量规则、规则信号和投票；依赖配置与已采集输入。 |
| `enrich/` | 模型输入包、prompt/schema、缓存、引文与数字核验、单元格核验及来源关联；检查通过不证明自然语言判断一定正确。 |
| `load/`、`schema.py`、`schema_app.py` | 管线表写入、应用 ID 映射和两套表结构；BFF 用户状态不属于管线所有权。 |

## 5. Data Flow and Data Contracts

1. 采集响应先存到原始存储，随后形成公司、申报、事实、章节等管线数据。
2. 计算按公司财年和指标定义取值，记录来源、期间、单位与 derivation。文本抽取还检查
   证据；表格指标由模型提议坐标，`enrich/table_reader.py` 验证单元格、期间、行列口径。
   `quality/identities.py` 核对配置指定的数值关系，不提供对所有财务含义的证明。
3. 洞察与问题保存版本及关联来源。人工 REJECTED 信号从后续洞察输入排除；已有洞察不会因
   写入人工决定就自动重新生成或重新发布。
4. `run_publish.py` 读取管线库，在应用库按公司事务写入；映射和 confidence 规则在
   `config/app_publish.yaml`。这不是跨库、跨公司或整个 run 的单一事务。

对外表/读取约定以 [docs/schema-contract.md](docs/schema-contract.md) 的**应用库部分**及
`schema_app.py` 为核对入口。该文档后半部的内部表仅供审计排错；历史待对齐项不应覆盖后续
D-037/D-040 的实现事实，也不证明真实 BFF 或 Azure 环境已验收。
本次选定承诺登记在 `capability_contract.json`，不从该 JSON 生成运行接口。

## 6. State and Persistence Model

- 默认 `DATABASE_URL=sqlite:///data/uw.db`、`APP_DATABASE_URL=sqlite:///data/app.db`；
  原始文件在 `DATA_DIR/raw`，报告在 `DATA_DIR/reports`。`.env` 与 `data/` 被 Git 忽略。
- `pipeline_run` 保存运行，`pipeline_task` 保存公司/阶段任务。
  任务经历 PENDING → RUNNING → SUCCEEDED / FAILED / SKIPPED。
  同库存在新鲜心跳的任意 job 会拒绝新的启动；这是当前实现的应用层检查，不能据此承诺
  多进程同时抢占的数据库级互斥。当前执行器按阶段遍历公司，任务串行执行。
- 仅中断后仍为 RUNNING、已过心跳期限、同 job/阶段/公司列表且在续跑窗口内的运行可恢复。
  默认心跳期限 30 分钟、窗口 12 小时、任务尝试上限 2；以 `config/jobs.yaml` 为准。
  已成功任务保留，SKIPPED 重新评估；已经结束的 PARTIAL/FAILED 不是这一续跑路径。
- 原始响应按资源及 UTC 获取日缓存；定时 SEC 刷新和手动 `--force` 控制重新获取。
  `--force` 不是清空所有缓存的总开关：`enrich/llm.cached_call` 仍按 task、prompt/guard 版本、
  模型、effort、system/user 与 schema 的哈希复用合格缓存。
- `llm_call` 在 SQLite 中参与调用方事务；服务端数据库另开事务保存，以免后续任务失败丢失
  已付费回答。`pipeline_publish_map` 位于应用库，不应被当成可随意清空的临时缓存。

## 7. Error and Failure Model

`run` 正常成功返回 0，PARTIAL/FAILED 返回 1，已有活跃运行时返回 2；参数解析错误也可返回 2，
需要结合错误文本区分。配置/初始化异常并不保证都会转换成上述运行摘要。
编排器把任务异常转成失败记录，按上限重试；包含 premium、quota、not set 等永久原因的失败
不立即重试。失败只阻塞同公司、同次运行的相关依赖，其他公司继续。

无模型配置时 signals 可走规则；textmetrics、governance、scores、insights 返回失败。
metricdetail 的规则部分单独调用可运行，但同次运行若包含且未完成 insights，它会被依赖检查跳过。
模型单项拒绝、截断、无效输出通常被扣下；setup 错误与各阶段的降级处理不同，不能承诺全局兜底。
治理阶段没有 proxy 或没有可用答案时也可能以说明文本成功结束，因此任务成功不证明取得了新事实。

`SUCCEEDED` 仅汇总任务执行结果；DQ Error 是数据，任务仍可成功。publish 成功只证明本次应用写入，
不会自动确认所有上游数据新鲜、DQ 无 Error 或 BFF 展示正确。
预算按配置价格与 token 数估算，并在新调用前检查；缓存仍可读，单次调用可使累计值越过预算，
这不是供应商账单级硬上限。规则、双模型同意和引文存在都不能消除共同误判。
抽取与复核是两个角色，不保证两个独立模型：若在 `config/llm.yaml` 中将两个角色配置为
相同模型规格，且完整缓存键相同，就会复用同一缓存回答，不能据此声称有独立第二意见。

## 8. External Dependencies, Authentication, and Configuration

| 边界 | 配置与凭据 | 缺失或部署前提 |
|---|---|---|
| SEC EDGAR | `Settings.sec_user_agent` 要求联系邮箱；限速、超时与重试见 `settings.py` | 即使部分本地命令也会构造 Settings；没有合格 User-Agent 会在初始化时报错。 |
| Alpha Vantage | 环境变量 API key，速率/每日额度；公司历史 symbol 在 `companies.yaml` | 缺 key 时 transcripts/news 任务失败；付费接口及实际账户权限需另行验证。 |
| 模型服务 | `config/llm.yaml` 选择 profile；各客户端配置定义网关与模型；凭据来自环境变量或忽略的 `.env` | `LLM_ENABLED=false` 不保证全量作业成功；模型和真实网关可用性需实际 smoke 验证。 |
| Azure SQL / ADLS | `DATABASE_BACKEND`、`STORAGE_BACKEND`、`config/azure.yaml` 与各服务主体凭据 | 配置含待填写环境值；SQL Server 需 ODBC Driver 18，Azure 依赖在 `.[azure]`。两类表需不同 schema。 |
| 构建/部署 | `pyproject.toml`、Dockerfile、`deploy/openshift/`、Harness skeleton | Python 支持声明为 >=3.11，开发/镜像使用 3.14；部署占位符与真实平台执行须分开验证。 |

## 9. Artifacts and Side Effects

已提交的 fixtures、golden 和 `deploy/sql/` 是可审查材料；golden 不等于本轮真实模型结果。
普通运行会发网络请求、产生模型费用、写原始存储与数据库。`init-db` 不迁移服务器数据库：
管线库非 SQLite 时退出 1；管线库为 SQLite 时先初始化它，随后只在应用库也为 SQLite 时
初始化应用库，否则跳过该步骤并正常返回。成功退出不能证明非 SQLite 应用库已就绪。
各分支见 [interact.md](interact.md#3-cross-cutting-user-visible-invariants)。数据库 DDL 由 `scripts/export_app_ddl.py` 导出，
共享环境迁移按既有运维约定审阅执行。测试隔离及清理见 [TESTING.md](TESTING.md)。

## 10. Extension Points and Architecture Debt

现有扩展点是配置中的公司/指标/行业 traits、带版本的 prompt、`LLMClient` / `Storage` 接口及
阶段分发/依赖配置。新增阶段也要核对 CLI 的 Stage 与默认顺序，不能只加一个函数。
`docs/PLAN.md` 的扩围、并发 worker、Batch 和 Phase 15/16 内容仍含规划与待确认项，不能当作已交付。
当前长事务、应用层启动检查、全局运行与预算假设需要在并发方案实施时重新验证。
生产部署、外部 BFF 对接和迁移职责仍按各自证据确认，不从单元测试推导。

## Change propagation

只列真实调用关系中容易遗漏的部分；按相关变更调查，无需每次全查。

| 变更类型 | 需要继续调查的影响面 | 相应验证 |
|---|---|---|
| CIK、历史主体、财年、行业/traits、指标或单位 | normalize → registry/compute → textmetrics → insight 输入 → publish/coverage/DQ；展示指标与仅供洞察的指标不同 | TESTING §4 的身份、财年与指标证据 |
| prompt、模型配置、schema、grounding | 调用缓存 → 任务校验 → 当前洞察版本 → metricdetail/关联 → 应用发布；guard 或 prompt 变化会改变缓存输入 | TESTING §4 的模型与缓存证据；原运维版本规则 |
| 增删阶段或改变执行顺序 | CLI、StageRunner、DEPENDS、jobs 配置、同次运行失败传播及独立重跑前提 | 编排与 CLI 场景；不把各阶段分别成功当成完整运行成功 |
| 管线/应用 schema、版本 ID 或发布字段 | upsert/AppWriter、关联、DDL、BFF 读取与用户引用 | publish 场景；涉及 DB 代码时补 SQL Server 集成与真实接口对齐 |
| 人工复核 | review_decision → 后续 insight 输入 / DQ 队列 → 重建洞察并再次 publish | 复核测试；已有应用输出不是即时撤回 |
| 新文件或正文中的发布受限词 | main 分支的发布脚本扫描全部 tracked 文件；main 中的 `scripts/prod.patch` 同步转换开发入口及文档引用 | 按 TESTING 的发布入口生成 prod 内容，核对引用、受限词扫描和本地检查；main 文档检查不能代替生产转换验证 |
