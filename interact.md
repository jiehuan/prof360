# User-visible Behavior and Acceptance

## 0. Authority and Scope

本页展开 CLI 调用者、数据使用者和集成人员能观察到的行为；正式入口目录统一见
[用户指南](docs/business_user_guide.md#0-documentation-and-interface-entrypoints)。
既有政策、表约定与描述性事实的区别见 [Authority Map](AGENTS.md#authority-map)。
应用表写入在本仓库实现，前端/BFF 的实际展示和验收不在这里证明。

<!-- capability-anchor: DOC.interact -->

## 1. Audience and Granularity

按“运行一次任务、发布可消费结果、检查质量、处理争议”组织场景。命令需要在项目根目录运行，
完成安装和 Settings 所需配置；只选一个阶段时，调用者负责其已落库输入。
这些行为是当前实现观察，长期承诺以 `capability_contract.json` 所登记的依据和原正式规范为限。

## 2. Current behavior

### 运行指定公司或定时作业

<!-- capability-anchor: CAPABILITY.configured_pipeline -->

- **User goal**：刷新已配置公司，或重新计算其中一个阶段。
- **Required context**：`config/companies.yaml` 中的公司、合格的 SEC_USER_AGENT、数据库和所选阶段的凭据/输入。
- **User action or request**：`uw-pipeline run --ticker MAR --stage compute`；
  全部活跃公司用 `--all`，定时配置用 `uw-pipeline run --job nightly`。
- **Directly observable result**：输出 run ID、SUCCEEDED/PARTIAL/FAILED 和任务计数；失败/跳过任务列出公司、阶段、原因。
- **Failure / degradation / escalation**：非 job 用法必须且只能选择 `--all` 或 `--ticker`；未知 ticker/job 被拒绝。
  `--job` 与 `--stage` 互斥。重复 `--stage` 按所给顺序执行，不自动补依赖。
  job 默认可恢复符合条件的中断运行；已成功的任务不重做。
- **Acceptance assertion**：核对退出码和任务摘要；成功为 0，运行未全部成功为 1；
  已有活跃运行和参数错误都可能为 2，应读取解释。`--force` 不承诺绕过 LLM 缓存。
- **Evidence**：`cli.py:run/_select_companies`、`tests/test_company_stage.py`、
  `tests/test_orchestrator.py::test_resume_after_crash_runs_only_unfinished_tasks`。

### 读取财务指标及来源

<!-- capability-anchor: CAPABILITY.sourced_metrics -->
<!-- capability-anchor: BOUNDARY.validation_scope -->

- **User goal**：获得可追溯的公司指标，判断缺口和初步值。
- **Required context**：对应公司已完成所需采集与计算；应用使用者读取正式应用表约定。
- **User action or request**：通过 BFF 集成约定消费已发布表；运维用 `uw-pipeline coverage-report --ticker MAR` 查缺口。
- **Directly observable result**：指标附带期间和来源类别；新闻稿补充值标记 PRELIMINARY；
  无法通过核验的候选可能不进入结果。覆盖率命令写报告并列出没有说明的缺口。
- **Failure / degradation / escalation**：缺值不等于零；NM 不等于普通缺失。
  引文存在、数字出现、模型同意或表格单元格核对通过，不保证业务解释无误；
  需要核对原文件的期间、单位、公司范围和适用口径。
- **Acceptance assertion**：XBRL 已覆盖期间不被初步文本值覆盖；
  展示转换和字段约定读 `docs/schema-contract.md` 的适用范围，不能将内部表当成 BFF API。
- **Evidence**：D-032/D-042/D-044；`tests/test_text_metrics.py::test_stage_writes_preliminary_and_crosschecks`、
  `tests/test_table_reader.py::test_wrong_cells_are_withheld`、`tests/test_phase2_golden.py`。

### 发布到应用数据库

<!-- capability-anchor: CAPABILITY.app_publish -->
<!-- capability-anchor: RESPONSIBILITY.bff_owned_state -->

- **User goal**：让外部应用读取管线已产出的当前数据。
- **Required context**：管线输入和可写的应用库/schema 已准备好；核对 `APP_DATABASE_URL` 或 Azure 配置。
- **User action or request**：`uw-pipeline run --ticker MAR --stage publish`，或对活跃公司使用 `--all --stage publish`。
- **Directly observable result**：CLI 报告任务结果；映射和应用行仍存在的同一对象按 ID 更新，内容未变时避免重写；
  KeyInsights、StrategicRisk、FollowUpQuestions 的新版本另建行并保留不活跃旧行，调用方按
  `isActive=1` 读取当前版本。其他表可删除过期行；外部删行后重建可能获得新 ID。
- **Failure / degradation / escalation**：写入按公司事务；失败不承诺回滚整个运行中其他公司的写入。
  publish 成功不代表模型内容刚刷新，不代表 DQ 无错误，也不代表 BFF 已接入。
- **Acceptance assertion**：重复发布不增加重复对象；旧问题 ID 不被复用成另一段问题文本；
  管线不接管 logo、收藏、最近访问或用户 flag。
- **Evidence**：D-037、`docs/schema-contract.md` 的应用表写入/读取约定；
  `tests/test_publish.py::test_republish_is_idempotent_and_new_versions_retire_old_rows`、
  `test_question_flag_target_is_stable`。

### 检查质量并做人工复核

<!-- capability-anchor: CAPABILITY.named_review -->

- **User goal**：找到质量问题和争议项，留下可追溯的人工决定。
- **Required context**：已有 DQ 结果或待复核信号；先读取 `review` 返回的当前条目 ID。
- **User action or request**：`uw-pipeline dq-report`、`uw-pipeline review`，
  再用 `uw-pipeline review reject <id> --reviewer '姓名' --note '依据'`（或 approve）。
- **Directly observable result**：DQ 写入报告；review 列出争议信号/恒等式错误及证据；成功决定输出结果和署名。
- **Failure / degradation / escalation**：未指定 `--run-id` 时，dq-report 查找最新的成功 DQ 任务；
  找不到才返回 1 且不生成报告。显式指定 `--run-id` 时直接查询该 ID 的 findings，不先验证运行
  是否存在或是否执行过 DQ；查询为空也会正常返回并写出 `Severity: no findings`。
  该文字仅表示没有查到发现，不能作为这一轮执行并通过质量检查的证据。
  无待复核项时 review 显示 Nothing to review。
  空署名或无效/已处理 ID 被拒绝并给出下一步。恒等式条目的 approve/reject 记录处理决定，不改写数值。
- **Acceptance assertion**：被拒绝的信号退出后续洞察输入和复核队列；不会即时删除既有洞察或应用内容，
  后续需按任务输入重新生成及发布。DQ Error 不自动阻止发布，也不使 dq 任务失败。
- **Evidence**：D-045；`tests/test_governance.py::test_review_needs_a_name_and_a_valid_id_and_rejected_signals_leave_the_input`、
  `tests/test_dq.py::test_dq_stage_end_to_end_and_rerun_replaces`、`cli.py:dq_report/_decide`。

## 3. Cross-cutting User-visible Invariants

<!-- capability-anchor: BOUNDARY.company_identity -->
<!-- capability-anchor: BOUNDARY.local_schema_init -->

公司选择依据配置和固定 CIK，原 ticker 别名可映射到同一主体；未知 ticker 不静默新增。
`init-db` 不承担服务器数据库迁移。当前 CLI 对管线库和应用库的处理不同：

| 管线数据库 | 应用数据库 | 当前行为 |
|---|---|---|
| SQLite | SQLite | 依次初始化两者；完成时正常返回。 |
| 非 SQLite | 任意类型 | 提示使用 `deploy/sql/` 和运维入口，退出码 1；不进入应用库初始化。 |
| SQLite | 非 SQLite | 先初始化管线库，再跳过应用库初始化；正常返回，不给出非 SQLite 应用库的拒绝提示。 |

成功退出不证明非 SQLite 应用库已就绪。底层 `ensure_schema` 自身拒绝非 SQLite engine，
但 CLI 在第三种配置中不会对应用库调用它；不能把底层保护概括成 CLI 对两套数据库的统一拒绝。
`SUCCEEDED`、publish 成功、DQ 报告和业务验收分别表达不同事实，不能互换。
证据为 `tests/test_company_stage.py`、`clients/db.py:ensure_schema`、`cli.py:init_db` 及上列场景测试。

## 4. Known Limits and Human Escalation

- 无模型时 signals 可退回规则，metricdetail 的规则部分可单独运行；textmetrics、governance、scores、
  insights 不能据此成功完成。完整作业仍可能 PARTIAL，不能把 `LLM_ENABLED=false` 当作全量离线模式。
- 没有 AV key 时 news/transcripts 失败；已有 key 也不证明账户拥有付费接口或每家公司都有记录。
- 预算是配置价格估算；超限后新的模型请求被扣下，缓存仍可用。不能承诺账单硬封顶或下一天一定补齐。
- 抽取/复核角色是否使用独立模型取决于 profile；相同模型规格及输入可复用同一缓存回答，角色数不是独立意见数。
- 无 proxy/无合格回答可不产生新董事会事实；“无信号”“未取得数据”“沿用已有结果”应区别解释。
- 并发扩容和 Phase 15/16 的新数据源/功能仍按 `docs/PLAN.md` 的状态处理。
- 数据口径、BFF 契约和生产迁移问题交由相应维护/集成人员按原文档确认；仓库未配置专门支持 SLA。

## 5. Combination Behavior

| 组合 | 当前可观察结果及依据 |
|---|---|
| compute 成功、LLM 阶段失败、publish 被选入 | publish 仍可写当前存储结果；整个 run 可以 PARTIAL。`DEPENDS` 不是“所有前序阶段都必须成功”。 |
| 同次运行 insights 失败，同时选择 metricdetail | metricdetail 被跳过；单独选择它并已有所需输入时，规则部分可运行。单测独立调用成功不等于组合成功。 |
| XBRL 与新闻稿提供同一期间指标 | 结构化值优先；文本作为核对或补缺，PRELIMINARY 不覆盖已有 XBRL/NM。 |
| 人工拒绝信号、已有洞察已发布 | 后续输入排除该信号；已有内容不自动撤回，需重建及发布。 |
| DQ 发现 Error、publish 已完成 | 质量问题可查询，但不会自动回滚已发布数据。 |
| 显式查询不存在或未执行过 DQ 的 run ID | dq-report 可正常写出 `no findings`；需要另查运行及 DQ 任务状态，不能把空结果当成检查通过。 |

相应实现见 `jobs/orchestrator.py`、`run_text_metrics.py`、`run_dq.py`、`enrich/package.py`；
证据层级及尚未覆盖的组合见 [TESTING.md](TESTING.md)。

## 6. Open questions

### BFF 的 Risk Flags 统计口径

**Current evidence**：`docs/schema-contract.md` 的历史内部表对照写 Warning/Critical 计数，
末尾仍保留“是否包括 Info、是否排除 OVERRULED”的待对齐项；
当前公开应用表映射在 `schema_app.py`、`run_publish.py`，本仓库没有 BFF 计数实现。
D-045 的人工复核改变后续输入，不自行决定外部页面的统计政策。

**Current behavior / safe boundary**：管线可输出相关风险对象；不能仅凭现有内部 signal 表说明
向业务用户承诺外部页面的 Risk Flags 数量。使用已对齐的应用表，争议统计交由接口对齐处理。

**Decision**：沿用 `docs/schema-contract.md` 的待对齐入口及 `docs/decisions.md` 的决定记录。
本仓库可见材料不足以确认外部 BFF 已作出的决定，不另造负责人或审批流程。

**Close when**：正式表契约写清计数和过滤规则，实际 BFF 消费者及对应集成验收有证据后，
把本项改为 Current behavior，并关联行为测试。

## 7. Interface Entrypoints

目录统一见[用户指南的 Documentation and Interface Entrypoints](docs/business_user_guide.md#0-documentation-and-interface-entrypoints)。
本页只展开交互，不建立第二份接口清单。
