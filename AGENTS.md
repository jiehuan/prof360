# Agent Working Guide — prof360

本项目将 SEC EDGAR 与 Alpha Vantage 数据整理为指标、信号和模型洞察，写入应用数据库，
供 Professional 360 BFF 读取。本文件统一维护人和 Agent 共用的开发约定；专题流程见下列入口。
开始任务时确认所用工具已读取本文件。若工具不自动加载 `AGENTS.md`，须在会话中显式要求
读取它；仓库里有此文件并不证明工具已加载规则。

## Authority Map

| 来源 | 范围与使用方式 |
|---|---|
| [本文件的开发约定](#project-specific-conventions) | 承接项目既有开发规则：阶段确认、配置归属、fixture/prompt/golden、数据库与 BFF 边界；规则正文在此维护，专题文档维护具体流程。 |
| [docs/PLAN.md](docs/PLAN.md) | 产品计划与 checklist；待确认/未来 Phase 不证明功能已实现。 |
| [docs/decisions.md](docs/decisions.md) | 已接受选择、理由和后续替代；后续决定只替代明确覆盖的范围，历史实测记录不等于当前环境验收。 |
| [docs/schema-contract.md](docs/schema-contract.md) | 应用表写入/读取约定；后半部内部表仅供审计排错，历史待对齐项要结合后续决定与实现核查。 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md)、[docs/TECHNICAL.md](docs/TECHNICAL.md) | 已有操作正文与技术详解；描述性声明仍须核对当前实现，旧命令不能自证正确。 |
| `pyproject.toml`、Dockerfile、`config/`、`src/`、`tests/` | 实际入口、机器检查、配置及行为证据；机器配置不自动取消项目政策。 |
| 本次接入的九份工作流文档 | architecture 维护当前结构与传播关系；TESTING 整理验证及证据规则；contract 登记选定承诺；interact 展开行为；用户指南导航；Checklist/SOP/PR template 组织交付。它们不新增产品能力或覆盖上列正式约定。 |

事实不清继续调查；明确的后续决定按其作用域处理。仍存在公共语义冲突时保留可核对的依据，
通过既有决定入口处理，不用代码偶然行为创造长期承诺，也不用政策句式掩盖实现差异。
现有文档中的“已验证”必须区分历史记录、本次可重复结果和外部验收。

## Task Routing

| 任务 | 优先入口 | 随后核对 |
|---|---|---|
| 局部实现、修复 | TESTING.md、相关源码 | 最近边界的测试、配置与失败路径 |
| 用户可观察行为 | interact.md、TESTING.md | CLI / 应用表及其真实消费者 |
| 公共能力或数据约定 | capability_contract.json、docs/schema-contract.md | architecture、interact 与应用映射 |
| 共享客户接口、缓存、schema | architecture.md、TESTING.md | 实际调用方与事务、状态、错误边界 |
| 项目使用、外部调用 | docs/business_user_guide.md | 其中集中维护的正式接口目录 |
| 合并、发布 | PR_Checklist.md、PR template、SOP.md | 实际 diff、原发布流程和目标环境 |
| 未决公共语义 | interact.md 的 Open questions、docs/decisions.md | 当前授权、正式约定和相关实现 |

日常任务按影响选择入口，无需每次读完九份文档。内部实现有多种等价方式时选最简单且符合
项目风格的方案；只有已调查清楚、仍超出授权的实质性公共语义分歧才提出具体问题。
传播关系只维护在 [architecture.md](architecture.md#change-propagation)，测试证据见 TESTING。

## Repository Overview

### Core Configuration

`settings.py` 读取环境变量/忽略的 `.env`；`.env.example` 给出配置入口。
`config/companies.yaml` 固定公司身份；industry/concept/metrics/overrides 管理口径；
jobs 管任务选择，llm/text_extraction/insights/scores/signal_rules 管模型和业务规则，
app_publish/source_labels 管输出映射与来源，dq_rules/coverage_notes 管质量与缺口。

### Runtime Entrypoints

`uw-pipeline` → `uw_pipeline.cli:app` → Orchestrator / StageRunner；正式接口目录在用户指南维护，
操作命令见 docs/OPERATIONS.md，验证命令见 TESTING.md。
Dockerfile 入口也是此 CLI。OpenShift 和 Harness 文件需要填写部署环境，不证明已上线。

### Core Modules and Domain Logic

`clients` 管外部访问；`extract/normalize` 处理来源；`compute/quality/signals` 执行配置规则；
`enrich` 组织模型任务与校验；`jobs` 协调阶段；`load` 写入两套 schema。
财务公式和行业指标集合在配置中维护，规则依据与边界见 architecture、原决定及对应测试。

### Generated Artifacts and External State

已提交的 fixtures/golden/SQL DDL 可审阅。`data/` 的原始文件、SQLite、报告与模型缓存是运行状态；
本地原始响应默认落在 `data/raw/`，管线库默认是 `data/uw.db`；生产可用 SQL Server/Azure SQL
和 ADLS。凭据不进入代码、日志或 fixture；发布会写应用库，
人工决定会改变后续模型输入，外部应用仍有自身数据所有权。

## Collaboration

执行者负责核实委派结论与最终写入；多人操作先划分不重叠路径，数据库、额度和共享环境操作
统一协调。调查和审查默认只读；独立审阅须说明真实上下文来源，不能把共识当成项目证据。
本项目没有在 tracked 配置中指定固定 Agent 数量或分工，不为普通任务额外创建审批环节。

## Architecture

修改前沿真实入口找调用方、数据与状态边界，修改后核对失败传播和下游结果。
公司/阶段不同不自动意味着状态独立；当前运行记录、模型预算和应用映射有共享状态。
本地测试成功不能证明跨进程并发、BFF 页面或生产部署已通过。

## Testing

执行前读取 TESTING 的对应入口。沿用无业务网络的 unit tests、记录 fixture、原文 golden 和
prompt 版本规则；分别落实 bug-first 与 no-test-diff 证据。未经运行的检查不要填通过。
文档结构核对、模拟场景、真实 SQL Server、真实网关分别说明，禁止相互代替。

## SOP

稳定操作入口见 SOP.md，日常正文仍在 docs/OPERATIONS.md。执行记录按任务/交付渠道保存，
不向本仓库添加不需要的状态文件、自动化层或重复规范。

## PR Delivery

按 PR_Checklist.md 与 `.github/pull_request_template.md` 说明当前实际变化和验证。
未经明确要求，不 commit、push 或创建 PR；发布授权与文档编辑授权分开。
PR 目标分支按请求和实际远端解析，不把默认分支与特殊生产分支混同。
仓库未提供当前远端分支保护或合并权限的可核对配置，不声称这些 gate 已启用或已满足。

## Project-specific Conventions

以下承接项目原有开发约定，保留已接受决定在各自范围内的效力。命令、详细步骤与行为限制
由 TESTING、OPERATIONS、architecture 和 interact 分别维护，不复制另一份规则正文。

### 计划与范围

- `docs/PLAN.md` 是项目计划。每个新 Phase 开始前列出计划及拟改文件，等待确认；
  计划未覆盖的决定先在 `docs/decisions.md` 记录选项与理由，再继续执行。
  已授权的日常修复或文档同步不因此成为新 Phase；完成 checklist 项后更新 PLAN 中对应勾选框。
- 不修改外部 BFF 仓库；发现接口问题记到 `docs/schema-contract.md`，应用表与 BFF 自有状态
  的边界按该契约和已接受决定处理。

### 数据、配置与外部访问

- 公司身份由 `config/companies.yaml` 中固定的 CIK 确定；ticker 用于显示和选择，不能当永久身份。
- XBRL tag 映射、指标公式和行业规则放在 `config/`，不硬编码；行业指标清单在
  `config/metrics.yaml` 的 `metric_sets` 和 `config/industry_groups.yaml` 中人工维护，不在运行时生成。
- `coverage-report` 展示的每个指标缺口都须在 `config/coverage_notes.yaml` 中说明。
- 所有外部 HTTP、LLM、数据库访问经过 `src/uw_pipeline/clients/`，以便测试替换。
- 数据库写入必须可重复执行且不产生重复结果，使用自然键 upsert / MERGE；禁止全表删除后重插。
- 密钥只从环境变量读取（本地可由已忽略的 `.env` 提供），不写入代码、日志或 fixture。
  Alpha Vantage key 同样不得记录或存储；其错误消息由 `clients/alphavantage.py` 脱敏。
  遵守配置的调用速率和每日额度（客户端默认 25 次/日）；额度耗尽时停止相应请求，额度恢复后
  由后续运行重试，不能把它理解为本轮全部阶段成功。

### 模型、规则与回归

- 单元测试不得访问网络；每个新增外部调用须在 `tests/fixtures/` 提供记录下来的 fixture。
  模型测试使用 `tests/fake_llm.py`，不能调用真实模型 API。
- Golden 基准值必须来自申报原文（XBRL instance 或文档），不能来自估算。
- 模型调用经过 `enrich/llm.cached_call`，缓存包含输入哈希。输出必须通过结构校验；采纳为业务
  结果前还须通过相应的来源依据校验。`llm_call` 中缓存了结构合格回答，不代表它已被业务采纳；
  具体边界见 [architecture 的模型处理](architecture.md#6-state-and-persistence-model)。
- Prompt 位于 `src/uw_pipeline/enrich/prompts/{task}_v{N}.md`；任何修改使用新版本，不原地覆盖。
  按 OPERATIONS §6 更新配置、重跑相关公司、运行 `scripts/export_golden.py` 并审阅
  `git diff tests/golden`。Grounding 规则修改须提升 `jobs/run_insights.py` 的 `GROUNDING_VERSION`。
- `LLM_ENABLED=false` 关闭真实模型调用；能够独立运行的规则路径仍可执行，但不保证完整任务
  离线成功，阶段差异见 interact §5 的组合行为。
- 每次运行由 `pipeline_run` / `pipeline_task` 跟踪。新阶段须登记到 `jobs/orchestrator.DEPENDS`、
  `jobs/stages.StageRunner._stage` 和 `config/jobs.yaml`；还须按 architecture 的 Change propagation
  检查 CLI 阶段枚举、默认顺序与调用方，不能只增加阶段实现文件。
- 信号与章节规则放在 `config/signal_rules.yaml` 和 `config/company_overrides/*.yaml` 的
  `sections:`；每个发现的信号误报都须成为 `tests/test_signals.py` 的回归用例。

### 机器检查与证据

`pyproject.toml`、conftest 和 `.gitattributes` 分别定义 Python >=3.11、ruff 规则与格式、
mypy strict、pytest 范围及 socket 拦截、文本换行归一化。开发环境沿用 Python 3.14，
安装与四项必要检查见 [TESTING.md](TESTING.md#0-canonical-test-entrypoints)。
配置的限制不自动取消上面的开发政策。

本文件与当前分支 `docs/decisions.md` 中的已接受决定按各自作用域使用。早期“尚无 Azure 支持”
等历史记录不能覆盖后续实现，后续实现也不能证明真实环境验收。生产转换与发布步骤见 SOP。
