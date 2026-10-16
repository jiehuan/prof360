# Standard Operating Procedures

## Purpose and Authority

本页只提供稳定流程入口，操作正文仍在 [docs/OPERATIONS.md](docs/OPERATIONS.md)。
政策范围见 [AGENTS.md](AGENTS.md#authority-map)，测试命令与层级见 [TESTING.md](TESTING.md)。
日常命令、模型名和部署参数留在专题正文；本页列出生产发布入口。

## Available SOPs

| 场景 | Action | Authority / Source | Acceptance |
|---|---|---|---|
| 日常运行与中断恢复 | 选择已配置 job/公司，检查任务与质量结果 | OPERATIONS §1；config/jobs.yaml；interact 的运行场景 | 任务结果、失败原因和 DQ/coverage 各自核对，不能只看一个成功标记 |
| 新公司、指标或行业组 | 固定 CIK、配置口径并核实来源/缺口 | OPERATIONS §2–5；既有开发约定；config/ | 对应测试及实际受影响公司结果，缺口有说明 |
| prompt 或校验变化 | 按版本政策更新，审查来源与输出差异 | OPERATIONS §6；decisions；TESTING 的模型入口 | 新旧结果及 golden diff 可解释，未测 live 不作已验证声明 |
| 人工复核 | 从当前队列查看证据并记录有署名的决定 | OPERATIONS §10b；interact 的质量/复核场景 | 有效决定保存；后续输入正确过滤；已有应用结果需另行重建/发布 |
| 应用表对接 | 核对公开表、当前版本和关联，保留 BFF 自有数据 | schema-contract 应用库部分；schema_app.py；D-037 | 映射与 ID 测试及实际消费者验收分开记录 |
| 数据库/原始存储部署 | 填环境配置，审阅 DDL/权限，运行原 smoke 流程 | OPERATIONS §10；config/azure.yaml；deploy/sql/ | 真实目标环境结果；本地 init-db 不能替代服务器迁移 |
| 容器与计划任务 | 审查镜像、持久化路径和部署占位符 | Dockerfile；deploy/openshift/README.md；Harness skeleton | 实际构建/部署及目标环境运行证据，文件存在不是完成 |
| 生产分支发布 | 应用原 main→prod 转换、扫描、测试与发布流程 | TESTING 的生产分支发布入口；AGENTS.md 的开发约定 | 生成分支内容和文档引用正确，扫描与必要测试通过，按授权发布并读回 |

main 的 `scripts/release_prod.py` 会写 Git 历史，须有对应发布授权；命令入口如下：

- `.venv/bin/python scripts/release_prod.py --push`：生成、扫描、检查并提交生产树，再推送 prod。
- 失败工作树按原流程修复并提交后，用 `.venv/bin/python scripts/release_prod.py --refresh` 更新转换补丁；再次发布仍需重新检查。

## Failure, Rollback, and Escalation

失败时保留实际错误与运行/任务 ID，按已有恢复条件处理。未结束的中断 job 只有满足心跳、
窗口及相同输入列表才续跑；已结束的失败运行不是同一恢复路径。
数据库写入有自然键幂等和按公司事务，但整个 run 不自动回滚；不得用 DELETE-all-then-insert
修复。应用 ID 映射和用户引用不能当临时垃圾清除，数据库恢复依真实环境已有备份/迁移方案，
仓库没有可承诺的一键全局回滚。

main 分支的发布脚本遇到补丁冲突、受限词或测试失败时会保留其 worktree，按既有修复/refresh 流程处理。
新增或修改文档时，同步核对现有 prod 补丁；main 与 prod 均以 `AGENTS.md` 为开发约定正文，
无需生成另一份规则文件。生产文档须引用实际存在的来源，不能为绕过扫描而抹掉权威来源。
转换后的引用、受限词扫描和本地检查都要验证，
普通 main 文档验收不替代该步骤；本地转换通过也不证明目标环境部署已完成。

命令/配置问题依 OPERATIONS 排查；BFF 接口争议沿用 schema-contract，超出已接受政策的选择
沿用 decisions。未确认的真实权限、迁移分工和对外语义先明确边界，不猜测责任人或外部承诺。
