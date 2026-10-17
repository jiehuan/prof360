# Testing

## 0. Canonical Test Entrypoints

仓库根目录是默认工作目录。开发规则见 [AGENTS.md](AGENTS.md#project-specific-conventions)；命令依据 `pyproject.toml`、
`deploy/harness/pipeline.yaml` 和现有脚本；本文汇集其验证范围，不另建 runner。
Python 包声明 >=3.11；既有开发约定、Dockerfile 与 Harness skeleton 使用 3.14。
“兼容性声明 >=3.11”不等于每个解释器都有本轮测试结果，也没有已配置的多版本 CI 矩阵。

先按既有开发约定准备 Python 3.14 的 `.venv`，执行 `.venv/bin/pip install -e ".[dev]"`。
本地必要检查分别运行并分别记录，不用后一个检查覆盖前一个的失败：

| 名称 | Exact command | 证明范围 |
|---|---|---|
| Lint | `.venv/bin/ruff check .` | pyproject 选择的静态规则 |
| Format | `.venv/bin/ruff format --check .` | 格式一致性，不证明行为 |
| Types | `.venv/bin/mypy` | 配置为 strict 的 `uw_pipeline` 类型检查 |
| Local suite | `.venv/bin/pytest -q` | pytest 当前收集的 tests；默认本地 SQLite、HTTP/LLM 替身，MSSQL 未配置时跳过 |

`deploy/harness/pipeline.yaml` 是有占位符的构建/部署骨架；存在文件不证明远端 CI 正在执行。
当前 Git tree 未提供 GitHub Actions workflow 或必需检查配置的证据。
依赖只有下界而无 lock，验收记录应带解释器和工具版本；换环境后必须重新看结果。

条件入口与来源：

- 数据库代码修改：按 [OPERATIONS §10](docs/OPERATIONS.md#10-azure-sql-和-adlsd-040)
  使用 `scripts/test_mssql.sh --all`，完成后 `scripts/test_mssql.sh --stop`。
  只检查 SQL Server 专项时脚本不带 `--all`。这需要 Docker、ODBC 镜像、网络下载和可清理的测试服务。
- prompt/grounding 修改：沿用 OPERATIONS §6 的版本、重跑、golden 导出及人工 diff 审阅；
  实际模型运行有成本，不能用 FakeLLM 的 PASS 代替。
- SEC fixture 更新：`.venv/bin/python scripts/refresh_sec_fixtures.py` 从已有的 `data/raw/sec/`
  原始响应重建测试样本，写入 `tests/fixtures/`；先审阅数据来源与生成差异。
- 故障路径检查：`.venv/bin/python scripts/stability_check.py --faults MAR`；真实模型稳定性使用
  `.venv/bin/python scripts/stability_check.py --runs 3 MAR JPM`（有费用），范围见 OPERATIONS §10b。
- 真实部署/网关检查按 OPERATIONS §9–10 执行下表命令；记录环境、凭据权限及副作用，
  不能用历史 smoke 记录作为当次结果。
- 生产分支发布：沿用 main 分支的发布流程，执行补丁转换、受限词扫描与四项检查。
  main 中的具体入口是 `scripts/release_prod.py`，转换补丁是 `scripts/prod.patch`。
  该发布流程会创建 worktree、commit，带 `--push` 会写远端，**不是文档检查或只读测试命令**。
  文档变更也可能影响 prod 转换，见 architecture 的 Change propagation；需用实际补丁生成内容，
  核对生产入口引用、受限词扫描及原发布脚本的本地检查。

| 检查入口 | 环境与副作用 |
|---|---|
| `.venv/bin/python scripts/securegpt_smoke.py` | 公司网络内检查网关与模型，产生真实请求。 |
| `.venv/bin/python scripts/runware_smoke.py` | 该网关的凭据与余额；真实模型请求，不写业务数据库。 |
| `.venv/bin/python scripts/azure_smoke.py` | 公司网络内检查 SQL / ADLS 权限与读写，不改业务表。 |

DDL 重新导出见 OPERATIONS §10 的 `scripts/export_app_ddl.py`；模型比较见 §8。
这些入口依各自脚本写文件或访问服务，不属于默认离线 suite。

## 1. Testing Philosophy

既有开发约定要求 unit tests 不碰业务网络，每个新外部调用提供记录 fixture；
发现信号误报须变成回归用例；golden 值来自申报文件，不能由待测模型自证。
固定输入与真实外部服务要分开表述；失败、跳过、未运行和历史结果分别记录。
不增加仅复述实现的测试，不用更多测试数量掩盖真实边界仍未覆盖。

## 2. Test Layers and What Each Proves

| 层级 | 当前证据 | 不证明 |
|---|---|---|
| Unit / 数据约定 | normalize/compute、schema、配置、HTTP client 测试 | 当前外部数据、真实凭据和供应商可用性 |
| Scenario | company CLI、orchestrator 中断续跑、publish 稳定 ID、review 路径 | 任意阶段组合、BFF 页面或全量生产验收 |
| Golden / holdout | `test_phase2_golden.py`、SEC 原文 fixture、`test_table_reader.py` 的未用于最初规则的版式 | 任意未来公司、所有申报版式或模型稳定性 |
| SQL Server integration | `test_mssql.py`；`--all` 还替换多数测试数据库 engine | 真实 Azure 权限、ADLS 服务及全部 CLI SQLite 文件路径 |
| Live / smoke | 原有 smoke、stability 与模型比较脚本 | 其他环境/日期/模型；一次成功不覆盖其他失败尝试 |
| 文档机械检查 | JSON、路径、anchor 及人工来源核对 | 运行时逻辑、业务正确性或产品验收 |

`tests/golden/*.json` 是已提交的审查样本，不能声称有一个当前自动化 gate 穷尽比对它们。
本仓库没有名为 full validation 的统一验收器；不要给局部测试添加这样的含义。

## 3. Capability Contract Alignment

新增登记文件保留稳定 anchor_id 与其原协议。当前仓库 **Not configured** 自动化的九文档
anchor alignment 测试：pytest 没有消费这些文档或 `capability_contract.json`，不能宣称 CI 已验证它们。
文档结构可用临时只读检查核对；新增长期 runner 需有独立维护收益，不为说明文档虚构既有自动化。

文档审查递归收集 contract 所有 anchor_id，核对唯一性、规范引用和真实 test_anchor。
`test_anchor: null` 须有具体 `untested_reason` 和 `pending_since`；它登记缺口，不表示已通过。
anchor 存在只能证明结构引用，不能证明句子语义或真实环境的交付。
正式应用表契约仍须核对 `schema_app.py`、`docs/schema-contract.md` 应用库部分与 publish 测试。

## 4. Change Type to Required Evidence

先按 [Change propagation](architecture.md#change-propagation) 找受影响入口，再把风险转成输入、
预期结果与对应测试；这里维护验证证据，不复制传播关系。
沿用本次接入工作流的两项独立证据要求：

1. **bug-first**：可安全复现的行为缺陷先留下失败用例/fixture，再修复；无法稳定自动化时记录
   原失败证据、原因和限制。既有信号误报必须有回归用例的政策继续有效。
2. **no-test-diff**：没有测试变更时，指出具体已有测试覆盖了什么新风险并给出重跑结果；
   没有可覆盖的测试就如实记录缺口，不能只写“已有高层测试”。

可用证据映射如下；测试名称只表示该测试实际断言的范围：

| 风险/声明 | 输入、结果与已有证据 |
|---|---|
| 配置身份或未知公司选择 | ticker 被重新分配仍按固定 CIK；`test_company_stage.py::test_pinned_cik_beats_reassigned_ticker`、`test_unknown_ticker_rejected` |
| 财年推导、文本与结构化优先级 | 原申报 fixture 的 Q4 推导与幂等；`test_phase2_golden.py`；文本不能覆盖已存在的结构化期间，`test_text_metrics.py::test_stage_writes_preliminary_and_crosschecks` |
| 模型 schema / 缓存 / 表格拒绝 | 无效回答不复用、正确单元格保留而错误单元格扣下；`test_llm_cache.py`、`test_table_reader.py`、`test_structured_first.py` |
| 任务顺序、失败隔离与续跑 | 一家 facts 失败不阻塞另一公司，已完成任务不重做；`test_orchestrator.py::test_failed_prerequisite_skips_only_that_company`、`test_sigkill_and_restart_continue_the_run` |
| 应用映射与用户引用 | 重跑稳定、新版本停用旧行、用户 flag 指向的旧问题不换内容；`test_publish.py` |
| 人工决定与后续输入 | 缺姓名/无效 ID 拒绝，REJECTED 信号不再进入输入；`test_governance.py::test_review_needs_a_name_and_a_valid_id_and_rejected_signals_leave_the_input` |
| DQ 成功与质量发现不同 | 输入含质量问题，任务仍 ok 且保存 finding，重跑替换；`test_dq.py::test_dq_stage_end_to_end_and_rerun_replaces` |

纯文档变更若没有运行时代码或测试 diff，可用既有测试重证上述已覆盖实现；
新增文案、authority、链接及组合解释另需文档复核。不能说 pytest 证明了新文案。
新增长期测试时优先扩展现有 fixture/参数化/场景；对未被现有测试覆盖的真实行为变化再补测试。

## 5. Side Effects and Isolation

本地 suite 用 respx/FakeLLM 和 `tmp_path`/SQLite；编排测试会启动并终止测试子进程。
先确认未设置指向真实服务的 `MSSQL_TEST_URL` / `UW_TEST_ON_MSSQL`，不要读取或借用生产秘密。
pytest、ruff、mypy 和解释器可能留下已被忽略的 cache；测试后核对 tracked diff 与意外新文件。

SQL Server 脚本会创建固定名称容器、network、镜像及测试数据库；测试建/删 schema，不能指向
生产库。`--stop` 删除数据库容器和 network，但构建镜像可能保留，需要如实报告残留。
普通管线运行、golden 导出、stability/live smoke 的写入和费用依其脚本核对，不能当成免费本地测试。
每次记录 exact command、scope、result、not-run reason、environment、side effects 和 cleanup；
未运行的其他解释器/数据库/真实服务保持独立状态。

## 6. Test Suite Overview

`tests/` 覆盖客户接口、归一化/计算、阶段、编排、模型校验、发布与质量。
`tests/fixtures/sec/` 提供申报原件及 holdout；AV 数据有固定/合成样本；模型客户接口有已记录响应，
`tests/fake_llm.py` 提供确定性替身。运行 suite 以前先核对 conftest 的无网络与数据库替换规则。

## 7. Known Gaps and Untested Reasons

- 自动化 anchor alignment 未配置；人工/临时结构检查不构成长期 CI。
- 本仓库没有外部 BFF 的真实消费测试，Azure/网关/ADLS 真实验收与本地 suite 分开。
- `init-db` 的管线库拒绝、双 SQLite 初始化和非 SQLite 应用库跳过分支没有专门 CLI 回归，
  见 contract 中该项缺口；底层 ensure_schema 的保护不能代替这些分支覆盖。
- `dq-report` 默认查找失败与显式 ID 的空结果路径没有专门 CLI 回归；阶段生成 findings 的
  测试不证明命令会验证 run 的存在性或 DQ 执行状态。一次性控制流探针不等于长期回归覆盖。
- publish 在 LLM 失败后继续、同次与独立 metricdetail 的差异、人工决定后的整条重建/发布链，
  有相关组件测试和实现依据，但没有逐项专门的完整 CLI 组合测试；不得以组件 PASS 代替。
- 应用层运行检查没有多进程同时启动的原子互斥测试；规划中的 worker/并发不是已覆盖能力。
- 生产转换的本地扫描/测试不证明目标环境部署已验收；实际发布仍遵循原授权与运维流程。

## 8. Lessons Learned

以已有回归保存机制：身份要用固定 CIK；看似合理的表格数字仍需期间/口径核对；
已完成任务的中断恢复不能靠全部重跑；新问题版本必须保留旧用户引用。
分别见 company、table_reader、orchestrator、publish 测试，不把历史事故叙述代替这些可执行证据。
