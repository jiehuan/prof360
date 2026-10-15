脚本都在仓库的 deploy/sql/（main 分支）

【表结构改动】
管线在同一个数据库里用两个 schema：应用表（BFF 读的表，暂定 dbo_professional360）和管线自己的表（新建，暂定 uw_pipeline，BFF 不读）。两者必须分开，因为 SQL Server 表名不区分大小写，应用的 Company 和管线的 company 会撞名。

应用表的改动（app_schema_changes.sql）：
- 新表：ItemLink（详情页的 Related 关联）、CompanyMetricDetail（指标详情弹窗）、pipeline_publish_map（管线内部的 id 映射，BFF 不读）
- 新列：KeyInsights.isActive；FollowUpQuestions.Priority、isActive；LoanTypes.ParentSegment；LoanPortfolio.PeriodEnd、PctOfBook、Nonaccrual、NonaccrualRatio；NewsRiskScore.ActivityId；TranscriptRiskFlag.Category；TranscriptToneAnalysis.CompanyId、Quarter
- 新索引：KeyInsights 和 FollowUpQuestions（CompanyId, isActive）、LoanPortfolio（CompanyId, PeriodEnd），以及两张新表的索引
- 删外键：所有指向 alpha_Transcripts、alpha_run、alpha_NewsArticle 的外键

新列都允许 NULL；两个 isActive 是 NOT NULL DEFAULT 1，现有行都算当前版本。alpha_* 表由管线取代，以后没有程序写它们。引用它们的列（TranscriptsId、runId、News.ArticleId）现在存管线自己的 id，外键不删的话写入会失败。旧表删不删由你决定。
管线自己的表（pipeline_schema_full.sql）：36 张，全部是新表。

【需要你做的】按顺序做，五步都完成后管线就可以第一次运行。
1. 确认配置值（dv/pp/pr 各一套，config/azure.yaml 里现在是占位值）：
   - Azure SQL 服务器和数据库名
   - 应用表的 schema：必须是 BFF 现在读的那个
   - 管线表的 schema：建议 uw_pipeline
   - ADLS 存储账户、容器、路径前缀：建议 professional360/raw
   schema 名和暂定的不一样时，改 config/azure.yaml 后运行 python scripts/export_app_ddl.py 重新生成脚本；或者把名字告诉我，我来生成。
2. 授权：为管线的服务主体建数据库用户（CREATE USER ... FROM EXTERNAL PROVIDER），在两个 schema 上授予 SELECT、INSERT、UPDATE、DELETE。运行时只用临时表，不需要建表或改表的权限。ADLS 的服务主体需要路径前缀下的读、写、删除和重命名权限。
3. 执行脚本（都可以重复执行，已在 SQL Server 2022 上验证）：
   a. pipeline_schema_full.sql
   b. 已有应用表的环境执行 app_schema_changes.sql；全新环境执行 app_schema_full.sql
   c. 用 app_schema_changes.sql 第 4 节的查询列出指向 alpha_* 表的外键，逐个 DROP CONSTRAINT
4. 把 AZURE_SQL_CLIENT_ID、AZURE_SQL_CLIENT_SECRET、ADLS_CLIENT_ID、ADLS_CLIENT_SECRET 放进 OpenShift Secret（模板是 deploy/openshift/secret.example.yaml）。请不要通过邮件或聊天发送。
5. 在公司网络里设好这四个凭证变量，运行：
   DATABASE_BACKEND=azure_sql STORAGE_BACKEND=adls AZURE_ENV=dv python scripts/azure_smoke.py
   确认连接、schema、表、权限和 ADLS 读写都通过。ALTER 和 CREATE TABLE 显示 WARN 是正常的。

【之后的约定】
- 表结构由你们执行，管线不会在共享环境里建表或改表。表结构有变化时，我会重新生成 deploy/sql/，把改动脚本发给你。
- 请不要手动修改或删除管线写入的行。被删的行下次发布时会以新的 id 重新插入，用户对旧 id 做的标记（比如追问问题的 flag）会对不上。
- 管线不碰用户相关的表（收藏、浏览记录、偏好），也不写 Company.logo。
- 请转告 BFF：KeyInsights、StrategicRisk、FollowUpQuestions 只读 isActive = 1 的行；来源类字段存 JSON 列表 [{"title","url","date"}]；字段对照见 docs/schema-contract.md。

【更新 2026-10-14（Phase 14）】
- 管线表新增 2 张：board_composition（董事会构成，从委托书读出）和 review_decision（人工复核记录）。共 36 张。
- 应用表没有改动，app_schema_changes.sql 和 app_schema_full.sql 不变。
- 如果已经执行过 pipeline_schema_full.sql：重新执行一次即可。脚本只创建不存在的表，已有的表和数据不受影响。权限沿用第 2 步，不需要新的授权。
- 应用库里 metrics 会多 3 行（ROA、ROE、贷款损失准备率），由管线发布时写入，不需要你处理。

【更新 2026-10-15：BFF】
- 本仓库新增 BFF（`bff/`，FastAPI），读应用库，并读写用户状态表。
- 用户状态表脚本：deploy/sql/bff_user_tables.sql，建在应用表的 schema 里，可重复执行。表名和团队数据模型图一致（Users、UserHistory、UserFavority、UserPinnedCompanies、UserFlaggedFollowupQuestions、UserSIC、UserMetricsSelection），字段是按表名推测的。如果这些表在你们的环境里已经存在，请先不要执行，把现有的表结构发给我，我按实际字段改。
- 请为 BFF 单独建一个服务主体和数据库用户：应用表只给 SELECT；用户状态表给 SELECT、INSERT、UPDATE、DELETE。凭证放 OpenShift Secret `bff-secrets`（BFF_SQL_CLIENT_ID、BFF_SQL_CLIENT_SECRET，模板见 deploy/openshift/secret.example.yaml）。
- 部署：镜像 `docker build --target bff .`，清单 deploy/openshift/bff.yaml（Deployment、Service、Route，健康检查 /health）。登录暂时是开发模式，接公司 SSO 前需要 IT 的 tenant ID 和应用注册。

