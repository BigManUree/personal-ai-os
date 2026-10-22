# 项目进度

更新时间：2026-10-22 13:25 CST

## 当前阶段

### 2026-10-21/22 全项目审查 + P0 修复 + 前端清理：已完成

- [x] E2 错误路径：`app/main.py` 三处 `detail=exc.message` → `str(exc)`；非法 action / status 由 500 变 400。
- [x] E1 设置接口：白名单改为以 `reminders.SETTINGS_FIELDS` 为唯一真源，忽略 `created_at`/`updated_at`，未知字段 400；`task_due_enabled`、安静时段等真实字段可写入并回读。
- [x] E3 租约时钟：`run_tick` 释放改用本次 tick 的 `current_dt`；`_lease_is_free` 与 acquire 条件 UPDATE 把 `released_at IS NOT NULL` 视为空闲。
- [x] E4 snooze：写入 `snoozed_until`，默认 10 分钟、上限 7 天，HTTP 支持 `snooze_minutes`；新增 2 个回归测试。
- [x] 时间炸弹：3 处硬编码 `resume_after_at` 改为相对未来时间。
- [x] E5 并发用例：新增 `rollback_journal` fixture，4 个并发用例首写前切 rollback journal；2 个 fencing 用例改为真实重叠断言；生命周期用例改 warm 连接与轮询。
- [x] WAL 决策：保留 WAL，README / `docs/database.md` 备份指引改为"停服后 db+-wal+-shm 或 `VACUUM INTO`"，`init_db()` 打印实际 journal_mode，记入 `docs/decisions.md`。
- [x] 前端设置页：`frontend/src/pages/Settings.vue` 改为后端真实字段（含安静时段、复盘时间、周几）并 `npm run build`。
- [x] 死代码清理：删除 7 个原生 JS（2,311 行）、`vite.config.ts` 改 `emptyOutDir: true` 并重新构建；`app/static` 现只含 `index.html` + 1 个 JS + 1 个 CSS（hash 随构建变化）。
- [x] 全量测试：`488 passed, 1 skipped, 1 warning`（修复前 `20 failed / 467 passed`）。
- [x] 产出审查报告 `docs/audits/2026-10-21-full-project-review.md` 与本批回退说明 `docs/handover/2026-10-21-fix-batch-revert.md`。

### 2026-10-22 建议采纳与生成入口（feature 002）：已完成

- [x] `Recommendations.vue` 重写：状态标签（进行中/待处理/已采纳/已忽略/已拒绝/已过期）。
- [x] pending 支持采纳/忽略/拒绝（拒绝内联二次确认 + 30 天冷却提示）；accepted 支持撤回采纳；终态只显示说明。
- [x] 动作失败展示后端 detail 并刷新列表；动作后局部合并 + 按需整表刷新。
- [x] 生成入口：窗口 7/14/30、`use_model`、服务端模型门槛说明、结果摘要（状态/窗口/缺口）且面板保持展开。
- [x] 反馈数据改 `Promise.all` 并行拉取，消除 N+1。
- [x] 规格与任务：`specs/002-recommendation-actions/{spec,plan,tasks}.md`，`.specify/feature.json` 指向 002。
- [x] 验证：`pytest -q` 488 passed / 1 skipped；Playwright 交互冒烟 11/11、生成冒烟 4/4；详见 `docs/testing.md` 第 0.2 节。

### 2026-10-22 项目 / 目标 / 任务写操作（feature 003）：已完成

- [x] 新增 `frontend/src/components/{Modal,ProjectForm,GoalForm,TaskForm}.vue`，`Work.vue` 由 51 行只读列表重写为三区块写操作页。
- [x] 项目：新建 / 编辑（5 字段）/ 按状态过滤 / 详情（事件 + 目标 + 任务摘要）/ 记事件（5 类型）/ 删除（说明"事件一并删除、任务与目标脱离"）。
- [x] 目标：新建 / 编辑（7 字段，含 `milestone` 与原生日期）/ 标记完成 / 恢复 / 放弃 / 删除（说明"任务解除目标关联"）。
- [x] 任务：新建 / 编辑（项目与目标下拉，目标按项目联动过滤）/ 开始 / 完成 / 重新打开 / 取消 / 删除 / 状态 tab / 过期标红。
- [x] `projectBody`/`goalBody`/`taskBody` 三个全量映射函数，规避后端 PATCH 全量替换导致的字段清空。
- [x] `tests/test_work_ui.py` 10 个测试：12 条写路由覆盖、三函数字段完整性、枚举一致、确认文案、无 `v-html`/`innerHTML`/`onclick=`，以及 3 个后端契约回归。
- [x] `frontend/e2e_work.mjs` 真实浏览器冒烟 36/36（两次一致），截图 `temp/e2e-work-filled.png`、`temp/e2e-work-final.png`。
- [x] 规格与任务：`specs/003-work-crud/{spec,plan,tasks}.md`，`.specify/feature.json` 指向 003。
- [x] 验证：`pytest -q` 498 passed / 1 skipped；真实库 sha256 与 mtime 未变；详见 `docs/testing.md` 第 0.3 节。

### 2026-10-22 建议结果登记与统计洞察（feature 004）：已完成

- [x] 新增 `frontend/src/components/OutcomeForm.vue`：accepted 卡片内登记执行状态（5 选）、效果评级（6 选）、备注（≤1000）、指标观察（≤10 条，指标 9 选 × 单位 3 选 × 期望/实际值），支持更新与删除（二次确认）。
- [x] 新增 `frontend/src/components/RecommendationInsights.vue`：窗口 7/30/90 天，展示推荐总数、状态分布、证据覆盖率、反馈率/有用率、采纳率、结果登记率、完成率、评级分布、指标对比表、`sample_status` 与 `limitations` 原文；附带最近 10 次生成历史表格。
- [x] `Recommendations.vue` 接入：顶部「统计与历史」折叠、accepted 卡片挂载 OutcomeForm、卡片加 `data-rec-id` 锚点、过期建议禁用保存。
- [x] 关键契约：outcome 的 `PUT` 是**部分更新**（与工作项 PATCH 全量替换相反）；`metric_observations` 整体覆盖；首次保存必须带 `execution_status`；非 accepted / 过期返回 409。全部用测试钉住。
- [x] 修掉一处实现偏差（E1）：统计接口的比率是 `{numerator, denominator, value, sample_status}` 对象或 `null`，不是标量；初版按标量渲染会让所有比率静默变「—」。
- [x] `tests/test_recommendation_insights_ui.py` 14 个测试；`frontend/e2e_insights.mjs` 冒烟 29/29。
- [x] 规格：`specs/004-recommendation-insights/{spec,plan,tasks}.md`（21 项全勾），`.specify/feature.json` → 004。
- [x] 验证：`pytest -q` 513 passed；真实库 sha256/mtime 未变；详见 `docs/testing.md` 第 0.4 节。

### 2026-10-22 个人画像条目管理（feature 005）：已完成

- [x] `Persona.vue` 由 45 行只读文本页重写为画像条目管理页：状态 tab（全部/候选/已确认/已过期/**已拒绝**）、新增/编辑弹层、确认/拒绝/过期/删除（后三者内联二次确认）、按条目展开修订历史、「生成推测」按钮（含 `LLM_API_KEY` 依赖说明）。
- [x] 新增 `frontend/src/components/ProfileItemForm.vue`：手动类别下拉固定 6 个（不含 `inference`），来源 6 种（不含 `model`），日期控件可清空。
- [x] 动作矩阵按 status 分发：pending→确认/拒绝；confirmed→过期/编辑/删除；expired→编辑/删除；rejected→只读说明。确认语义（候选升格为个人事实，`is_user_stated=1`）在 UI 中显式说明。
- [x] **修掉一处后端缺陷（E1）**：`PATCH /api/profile-items/{id}` 的 `valid_from`/`valid_until` 用 `is not None` 判断，显式 `null` 无法清空日期；改为 `model_fields_set` 判断（与 outcome 端点同模式），契约测试固定两个方向。
- [x] `tests/test_profile_items_ui.py` 18 个测试；`frontend/e2e_profile.mjs` 冒烟 23/23（播种 pending/confirmed/rejected）。
- [x] 规格：`specs/005-profile-items/{spec,plan,tasks}.md`（17 项全勾），`.specify/feature.json` → 005。
- [x] 验证：`pytest -q` 530 passed, 1 skipped；真实库 sha256/mtime 未变；详见 `docs/testing.md` 第 0.5 节。
- [ ] **flaky 升级**：`test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 本轮失败 3 次/通过 2 次交替，列为下一步排查优先项（见 `docs/testing.md` 0.5 末尾）。

## 本轮未开始（审查报告中的 P1/P2）

- 前端能力补齐：**审查报告中列出的前端缺口已全部补齐**（凭据管理=007、snooze/对比=006、画像=005、工作项=003、建议闭环=002/004）。Git 已推送且 **CI 三 job 首跑全绿**（ubuntu/windows/frontend，Linux 平台差异为零）；**main.py 已拆分**（3,495→821 行 + 9 个域 router + schema_migrations），88 路由与 569 用例一一对应。剩余：FTS5、聊天流式输出。
- 与上面连带：4 个前端断言原先校验已删除脚本的内部实现，现改为校验构建产物与 `frontend/src`（无 `v-html`/`innerHTML`、无 `onclick=`、产物与 `index.html` 引用一致）；feature 003/004 已把工作项与建议洞察的断言补回。
- 架构：拆分 `app/main.py`（3,461 行 / 88 路由）、引入迁移版本表（`PRAGMA user_version`）。
- 工程化：Git 初始化、CI、ruff/类型检查、前端测试脚本。
- 检索：FTS5 替换全表扫描；聊天流式与 evidence 预算。
- Spec Kit：现只有 `001`–`007` 七个 spec；phase 3–5 仍需补 spec 或明确豁免口径。

### 2026-10-14 第一轮书面裁定登记与 P0-4 候选方案研究：已完成（只读 + 文档更新）

- [x] 登记 R1 裁定：保留当前真实库，19:49 来源标记无法追溯（历史风险，不视为已解决）。
- [x] 登记 P0-1 正式接受（范围仅限 fencing 测试批次 7 passed）。
- [x] 登记 C-C-2d 有条件验收（fake 渠道 + 既有审查/测试；六项限制继承）。
- [x] 登记 P0-2 参数追认（10 秒 heartbeat / 30 秒 TTL / 边界检查续租 / 失败不伪装成功）。
- [x] 登记 P0-3 有条件追认（方案 C + 35 秒暂定值；四项未决验证事项记录在案）。
- [x] 只读核对 P0-2/P0-3 实现与测试覆盖（未修改代码）。
- [x] P0-4 现状澄清：`scheduled:{epoch//poll}` 桶键已在 C-C-2c 落地（此前文档描述滞后）；同桶首跑幂等（任何状态返回既有记录）。
- [x] P0-4 候选方案 A/B 对比与推荐已产出（会话内报告，未获批准，未修改代码）。
- [x] 更新 `docs/decisions.md`、`docs/progress.md`、`docs/current-task.md`、`docs/handover/current-session-handover.md`、设计文档状态标注。
- [x] 本批未运行 pytest、未启动应用/服务、未写真实库、未修改任何 Python 代码。

### C-C-2e-0 真实开发数据库变更来源只读审计：已完成（2026-10-13）

- [x] 固定当前真实库和两个备份的大小、时间、SHA-256。
- [x] 确认当前库无 `-wal`、`-shm`、`-journal` sidecar。
- [x] 使用 SQLite 只读 URI + `query_only=ON` 完成当前库和两个备份的 `integrity_check`、`foreign_key_check`、schema、索引、触发器、视图和行数检查。
- [x] 生成当前库/备份表清单、行数差异、共同表 schema 差异和关键业务状态计数。
- [x] 查明当前库包含 27 张表；缺少 C-C-2d 两张通知表和 lazy 创建的 lease 表。
- [x] 确认 `reminder_settings` 默认行时间等于数据库 mtime（UTC 11:49:11 / CST 19:49:11），所有提醒开关关闭。
- [x] 静态确认变更形态高置信符合 C-C-2d 前 C-C-2c 版本 `init_db()` 初始化；具体进程/操作者因缺少进程与命令日志仍无法证实。
- [x] 扫描当前及本地其他 Codex rollout、PSReadLine、PowerShell 事件和端口/进程状态，未找到 19:49 的项目命令。
- [x] 静态审查 `tests/test_reminder_scheduler_fencing.py`：测试均写入 `tmp_path` 临时库；20:10:11 的源文件修改来源无法确认。
- [x] 静态审查 C-C-2d：同事务 outbox、唯一约束、默认关闭、仅 `fake_test`、无真实通知 I/O、health 不暴露敏感值。
- [x] 新增 `docs/audits/C-C-2e-0-database-safety-audit.md`。
- [x] 更新本文件与 `docs/current-task.md`。
- [x] 审计期间未运行 pytest、未启动应用、未写真实库、未发送通知。

### C-C-2d 通知投递基础设施与幂等记录：已完成（历史动态测试结果）

- [x] 通知 event/delivery 两张表、数据库唯一约束和外键级联。
- [x] scheduled/snoozed -> due 与通知 outbox 在同一 reminder guarded transaction 中提交。
- [x] 稳定 event/delivery 幂等键，区分首次到期与不同 snooze 周期。
- [x] pending/running/retry_waiting/succeeded/dead_letter/cancelled 状态机。
- [x] 指数退避、超时/临时/永久错误分类、死信和 stale running 恢复。
- [x] 默认关闭的单线程 `fake_test` 消费服务；无真实邮件、短信、Webhook、桌面或云通知。
- [x] 历史动态验证：33 项通知专项通过；提醒/调度回归 124 项通过；全量 479 passed、1 skipped、1 warning。

### C-C-2c 自动提醒调度生命周期接入：已完成（默认关闭）

- [x] opt-in `ReminderSchedulerService`、FastAPI lifespan、独立 SQLite 连接、单线程 worker。
- [x] busy/lost/database_error/stopped 状态、health 快照、有界 shutdown。
- [x] SQLite lease/fencing 跨进程互斥和真实 OS 子进程测试。
- [x] 默认配置不启动 reminder tick；真实通知渠道未实现。

### 已完成的前置能力

- C-C-2a：提醒存储、规则、安静时段、状态机和手动确定性 tick。
- C-C-2b：SQLite 跨进程 lease、TTL、heartbeat、fencing、stale recovery。
- 工程化治理：临时数据库守卫、`AGENTS.md`、`pyproject.toml`、保守 `run.bat`、系统文档和测试依赖。
- ConnectorScheduler：逐数据源 opt-in 的本地 asyncio 同步循环。

## 当前阻塞 / 需人工确认

- **已知 flaky（2026-10-22 11:00 已定位并修复，见 `docs/testing.md` 0.6 节）**：根因是全新 WAL 库上调度线程首个写连接与其它连接的开/关竞争，Windows 收到瞬时 `SQLITE_READONLY`。修复为租约获取阶段的只读错误在 `run_scheduled_tick` 内换连接重试（至多 4 次、同一 run_key）；修复后复现脚本 14/14 通过、lifecycle 文件 8 次连跑全绿，`tests/test_reminder_scheduler_retry.py`（7 例）钉住契约。
- ~~P1：19:49 真实库来源~~ → 2026-10-14 已裁定：保留当前库，来源标记无法追溯（保留为历史风险）；继续禁止写入、初始化、迁移和应用启动。
- **P0-4 方案裁定**：桶粒度/时区边界/同桶失败重试/`recovery` 命名空间/停机补跑/`tick_once` 默认 run_key 语义待用户裁定（候选方案 A/B 已产出，见会话报告；未批准）。
- **P0-3 遗留验证任务**：35 秒宽限实测校准、进程退出独立验证、不杀线程与进程退出取舍、超时后同步 SQLite 状态风险。
- **R3 知情建表**：C-C-2d 两张通知表需在人工知情并备份后经一次应用启动幂等创建（未授权）。
- fencing 测试文件 20:10:11 的修改来源未知（R2，已告知用户，低风险）。
- P0-4 已实现部分的测试缺口：`scheduled:{bucket}` 键格式与同桶幂等尚无直接专项测试（属实现/测试批次）。

## 未开始

- 真实邮件、短信、Webhook、桌面通知或云通知发送器。
- 通知 HTTP API、通知中心、死信手动重放和管理界面。
- 手动 tick HTTP API。
- run/notification 历史保留和清理策略。
- 独立迁移框架/版本表。
- 将 `app/main.py` 按领域进一步拆分。
- connector 自身 `start()` 抛出部分启动异常时的回滚专项加固。

## 本轮只读验证状态

| 验证 | 结果 |
|---|---|
| 当前库 integrity_check | `ok` |
| 当前库 foreign_key_check | 0 行 |
| 备份 A integrity/foreign_key | `ok` / 0 行 |
| phase2 备份 integrity/foreign_key | `ok` / 0 行 |
| 当前库 WAL/SHM/journal | 未发现 |
| 当前库表/索引/视图/触发器 | 27 / 35 / 0 / 0 |
| 审计期间真实库指纹 | 未发生额外变化 |
| pytest | 按任务要求未运行 |
| 真实应用/调度/通知 | 未启动、未触发 |

历史 C-C-2d 全量测试结果仍为 `479 passed, 1 skipped, 1 warning`；它不是本轮 C-C-2e-0 的动态测试结果。

