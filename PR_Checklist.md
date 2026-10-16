# PR Submission Checklist

按 [Authority Map](AGENTS.md#authority-map) 核对证据与政策。只勾选有依据的已完成事项；
不适用写明原因，未配置与尚未确认分开。未经明确要求不 commit、push 或创建 PR。

## Scope and Git State

- [ ] 确认请求范围、工作分支、目标 base 及其实际远端状态；默认分支不等于特殊生产分支。
- [ ] 核对 `git status`、工作树/暂存 diff 及相对实际 base 的文件清单。
- [ ] 无秘密、本地数据、临时 PR body、cache 或未落地计划混入提交。
- [ ] 新 Phase 的计划/文件确认及未覆盖决定遵循已有开发约定；不为普通已授权工作重复审批。

## Tests and Evidence

- [ ] 依 TESTING 执行实际适用的本地检查，分别报告失败、跳过和未运行；记录解释器和工具版本。
- [ ] bug-first 与 no-test-diff 分别有证据；不新增测试时指出具体已有覆盖及其重跑结果。
- [ ] 记录 exact command、scope、result、not-run reason、environment、副作用和清理/残留。
- [ ] 数据库代码变更按原运维约定补 SQL Server 验证；真实网关/数据/部署未验证时不以 mock 替代。
- [ ] 新外部调用有 fixture；信号误报有回归；prompt/grounding 修改遵循版本和 golden 审查入口。

## Documentation and Contracts

- [ ] 按实际影响核对九份工作流文档及既有正式文档；保留内容有事实或政策依据，不为齐全制造 diff。
- [ ] 应用表与内部审计表区分，BFF 用户状态所有权和 schema/迁移边界明确。
- [ ] 未把 publish/任务成功当作 DQ 无 Error、数据全新、BFF 对接或生产验收。
- [ ] contract anchor 唯一、引用有效、测试指针真实；null 指针保留具体未测原因和日期。
- [ ] 不残留模板填充项；JSON 可解析；不把文档结构校验说成语义或运行时证明。
- [ ] 相关传播关系只在 architecture 维护；用户指南集中维护正式接口目录，其他文档引用。

## Review Closure

- [ ] review 记录对象版本、范围与实际上下文来源；不把继承作者上下文的复核称为独立审阅。
- [ ] 可执行反馈已修复并复核；超出范围的事实漂移有 exact path、风险及建议，不静默改写原规范。
- [ ] 公开语义有必要决定时用原 decisions/schema-contract 入口；已关闭问题同步行为与测试。
- [ ] 修改后重新核对相关测试、完整相邻段落和最终 diff。

## PR Delivery and Production Release

- [ ] 使用 PR template；正文反映最终变化、实际测试、已知限制与回滚边界，草稿不提交进仓库。
- [ ] 只在获得发布要求时执行对应 Git/GitHub 动作，核对 base、head、标题、正文和实际文件清单。
- [ ] 若任务涉及 `scripts/release_prod.py`：核对 main 到 prod 的现有补丁、全部文件名/正文扫描及四项测试，不把普通文档检查当作生产发布证明。
- [ ] 九个新增文件当前不在 `scripts/prod.patch` 的转换范围内；其中引用 `CLAUDE.md` 会触发受限词扫描，且 prod 将原入口改为 CONTRIBUTING.md。下次生产发布前需在既有发布流程中补齐转换并复核引用，不能仅删除词语破坏文档权威关系。
- [ ] 未执行目标环境、BFF 对接或生产发布时，明确保持未验证状态。
