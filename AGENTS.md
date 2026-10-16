# Agent Working Guide — prof360

## Authority Map

| 来源 | 范围与使用方式 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 已有开发约定、阶段确认、配置归属、fixture/prompt/golden 和 BFF 边界；本入口保留这些政策，不因新增文件改换权威。 |
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

`uw-pipeline` → `uw_pipeline.cli:app` → Orchestrator / StageRunner；具体命令只在用户指南目录维护。
Dockerfile 入口也是此 CLI。OpenShift 和 Harness 文件需要填写部署环境，不证明已上线。

### Core Modules and Domain Logic

`clients` 管外部访问；`extract/normalize` 处理来源；`compute/quality/signals` 执行配置规则；
`enrich` 组织模型任务与校验；`jobs` 协调阶段；`load` 写入两套 schema。
财务公式和行业指标集合在配置中维护，规则依据与边界见 architecture、原决定及对应测试。

### Generated Artifacts and External State

已提交的 fixtures/golden/SQL DDL 可审阅。`data/` 的原始文件、SQLite、报告与模型缓存是运行状态；
生产可用 SQL Server/Azure SQL 和 ADLS。凭据不进入代码、日志或 fixture；发布会写应用库，
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

以下是当前既有政策的路由，不另立第二套定义：

- **Owner-declared — CLAUDE.md、已接受 decisions，仓库开发范围**：新 Phase 先列计划/文件并确认；
  计划未覆盖的决定先记录选项和理由；完成 checklist 后更新 PLAN。已授权的文档同步不因此变成新 Phase。
- **Owner-declared — 同上**：固定 CIK；指标、公式、行业组由配置维护；每个 coverage gap 有说明；
  外部访问走 clients；业务模型输出按任务进行 schema/证据校验；数据写入保持自然键幂等；
  不修改外部 BFF 仓库，契约问题记到原 schema-contract。
- **Owner-declared — CLAUDE.md / OPERATIONS §6**：prompt 变化用新版本；grounding 变化提升
  `GROUNDING_VERSION`；依原入口导出并审查 golden diff；信号误报补回归用例。
- **Machine-enforced — pyproject.toml / conftest / .gitattributes**：Python >=3.11、ruff 规则与格式、
  mypy strict、pytest 范围及 socket 拦截、文本换行归一化；具体命令见 TESTING。

已核对上述来源及 D-037/D-040/D-042–D-047：不得从早期“尚无 Azure 支持”等历史记录覆盖后续实现，
也不得从后续实现推出真实环境验收。LLM 输出政策与缓存/业务表的区别见 architecture；
production 分支对文档文本的限制见发布 SOP。未把其他项目或个人会话偏好写成 prof360 产品政策。
