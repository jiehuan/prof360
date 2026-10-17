<!--
只写本 PR 已完成的事实。变更范围来自真实 Git diff；测试证据遵循 TESTING.md；用户行为和
架构影响分别核对 interact.md 与 architecture.md。不要把计划、历史草稿或本地未提交内容
写入 PR body。
-->

## 1. Background and Goal

<!-- 说明实际问题和结果。新的产品 Phase 遵循原开发约定；计划条目不能作为实现证据。 -->

## 2. Implementation and Tradeoffs

<!-- 说明核心实现、关键取舍、被拒绝方案及原因，不逐文件复述 diff。 -->

## 3. Actual Change Scope

<!-- 根据 git diff --name-only <base>...HEAD 列出实际变更；不要保留空表或计划中的文件。 -->

## 4. Documentation Impact

<!-- 按 AGENTS Authority Map 列出受影响文档；保留原开发、运维和应用表契约的权威范围。若新增文档影响 prod 转换，明确发布补丁边界。 -->

## 5. User-visible and Architecture Impact

<!-- 核对 CLI、应用表、当前版本与 BFF 自有状态。任务/publish 成功、DQ 质量及外部 BFF 验收分别说明。没有变化时写 None 并给核对依据。 -->

## 6. Testing Evidence

- Exact command：记录原样命令；未运行时写 `Not run`。
- Scope：按 TESTING 区分本地 suite、SQL Server、真实网关/部署及文档核对；无 test diff 时列具体已有覆盖和重跑结果。
- Result：记录通过、失败、跳过及关键数量或错误。
- Not-run reason：已运行时写 `Not applicable`；未运行时写具体原因和风险。
- Environment：记录实际执行环境、隔离方式、副作用和清理结果。

## 7. Review / Fix Record

<!-- 按本项目交付政策记录实际 review、可执行反馈、处理与复核结果，以及仍开放的决策或限制；格式和证据位置遵循项目政策。 -->

## 8. Known Limits, Open Decisions, and Rollback

<!-- 区分已知限制、未决公共语义和回滚。涉及 prod 发布时核对现有补丁对新增文档的转换与受限词扫描；不把本地测试称为生产验收。 -->

## 9. Final Self-check

- [ ] Actual Change Scope 与真实 diff 一致。
- [ ] 测试命令、范围、结果和未运行原因准确。
- [ ] 用户可见与架构影响已核对对应权威文档。
- [ ] 项目要求的可执行 review feedback 已处理，或已明确记录为开放项。
- [ ] PR body 不含历史草稿、未落地计划或错误 base/head。
