# 数据库说明

更新时间：2026-10-13 20:25 CST

## 1. 技术与位置

- 数据库：SQLite，本地单文件。
- 默认路径：`data/personal_ai_os.db`。
- 覆盖方式：环境变量 `DATABASE_PATH`。
- C-C-2c 交接基线为 258,048 bytes / LastWriteTime `2026-10-12 16:45:50` / SHA-256 `3EEB309...`。C-C-2d 开始后只读核验发现真实库在本会话代码写入前已为 352,256 bytes、LastWriteTime `2026-10-13 19:49:11`；该变化早于本轮 pytest，原因需人工确认，不能回滚或“修复”。
- 同目录还有两个历史备份：`personal_ai_os.db.bak-*`。本轮未删除、覆盖、迁移或重建这些文件。
- 只读 pragma：`user_version=0`、`journal_mode=delete`；SQLite 默认连接的 `foreign_keys=0`，应用连接会显式执行 `PRAGMA foreign_keys=ON`。
- 重要：`init_db()` 在每次启动时执行 `PRAGMA journal_mode = WAL` 与 `synchronous = NORMAL`，并打印实际模式。因此"当前库是 delete journal"只在应用尚未启动时成立；**一次启动之后库就变为 WAL**，备份语义随之改变（见第 6 节）。

## 2. 初始化与迁移

- HTTP 应用 startup 调用 `app.main.init_db()`。
- 常规 schema 使用 `CREATE TABLE IF NOT EXISTS`；旧库兼容通过 `_migrate_memories()`、`_migrate_documents()`、`_migrate_data_sources()`、`_migrate_recommendations()`、`_migrate_reminder_scheduler_runs()` 做增量调整；通知表由 `notification_delivery.SCHEMA_SQL` 幂等创建。
- 这些迁移是代码中的幂等辅助函数，目前没有独立迁移框架、迁移版本表或 down migration。
- `reminder_scheduler_leases` 由 `app.reminder_scheduler_lock.init_lease_db()` 在第一次获取租约时创建；本轮治理没有在真实开发库上调用它。
- 因未启动应用，本轮没有对真实开发库执行初始化或迁移。只读核验显示真实库当前有 22 张表；当前代码的完整临时 schema 有 28 张表，差异为尚未在该真实库中创建的建议反馈/提醒运行表和租约表。

## 3. 连接与事务

- 生产 HTTP 路径通过 `app.main.db()` 获取短连接：
  - `sqlite3.connect(DB_PATH, timeout=5.0)`；
  - `row_factory = sqlite3.Row`；
  - `PRAGMA foreign_keys=ON`；
  - `PRAGMA busy_timeout=5000`；
  - 上下文正常退出时 commit，最终 close；异常退出时依赖关闭未提交事务实现回滚。
- 领域模块大多接收调用方传入的 `sqlite3.Connection`，避免自行连接默认数据库。
- 自动调度连接由 `app.reminder_scheduler_service.run_scheduled_tick()` 单独创建；通知消费连接由 `notification_delivery.process_delivery_batch()` 单独创建，均设置 row factory、`foreign_keys=ON` 和 5000ms busy timeout，结束后关闭，不与 HTTP 连接共享。
- 调度租约写入使用 `BEGIN IMMEDIATE`。
- `run_tick()` 的受保护写入使用 guarded transaction：写入前和提交前都校验 owner、lease token、fencing token。提醒 due、通知事件和 fake delivery 在同一个 guarded transaction 中提交，通知插入失败会回滚提醒 due。
- 当前候选创建按每条独立 guarded 事务隔离；`savepoint()` 辅助仍存在但不是自动主流程边界。fencing/lease 失败会终止 tick，不能被候选级异常计数吞掉。
- 自动 tick 在阶段/批次边界 heartbeat、检查停止信号和 90 秒软上限；它不是一个大事务。

## 4. 表与关系（当前代码 schema）

下表来自当前代码在独立临时数据库中的初始化结果，不是对真实库内容的推断。

### 核心资料与对话

- `profile`：单例用户档案。
- `conversations` / `messages`：对话与消息；`messages.conversation_id -> conversations.id`。
- `approvals`：审批动作记录；当前只记录审批结果，不执行系统操作。

### 长期记忆与画像

- `memories`：正式记忆，含状态、有效期和 `supersedes_id`。
- `memory_candidates`：待人工审核候选，含重复/冲突指针。
- `memory_revisions`：记忆修订历史，`memory_id -> memories.id`。
- `profile_items`：个人画像条目。
- `profile_item_revisions`：画像条目历史，`item_id -> profile_items.id`。

### 文档和数据源

- `documents`：上传或录入文档的文本、hash、来源和解析状态。
- `document_chunks`：文档片段，`document_id -> documents.id`。
- `data_sources`：连接器配置与调度状态；密钥不放在 `config_json`。
- `source_items`：连接器外部条目，`data_source_id -> data_sources.id`，可关联 `document_id`。
- `connector_credentials`：Fernet 密文凭据，`data_source_id -> data_sources.id`。
- `sync_logs`：连接器同步日志，`data_source_id -> data_sources.id`。

### 项目、目标、任务、行为

- `projects`：项目。
- `goals`：目标，`project_id -> projects.id`。
- `tasks`：任务，`project_id -> projects.id`、`goal_id -> goals.id`。
- `project_events`：项目事件，`project_id -> projects.id`。
- `behavior_events`：最小行为事件，外键关联项目、目标、任务、数据源等。

### 建议与反馈

- `recommendations`：建议、证据、状态、去重键和有效期。
- `recommendation_runs`：建议生成运行记录。
- `recommendation_feedback`：用户反馈，`recommendation_id -> recommendations.id`。
- `recommendation_outcomes`：执行结果，`recommendation_id -> recommendations.id`。

### 提醒与调度

- `reminder_settings`：提醒规则、时区、周期回顾和安静时段设置。
- `reminders`：提醒实例；`task_id -> tasks.id`、`recommendation_id -> recommendations.id`。
- `reminder_scheduler_runs`：手动/恢复/计划 tick 的运行计数、错误和 fencing token。
- `reminder_scheduler_leases`：全局单例跨进程租约，包含 owner token、lease token、fencing token、TTL、pid 和 hostname。

## 5. 测试隔离

- `tests/conftest.py` 在 pytest 配置阶段强制把 `DATABASE_PATH` 指向系统临时目录，同时设置临时 `PAOS_SECRET_KEY_PATH`，清空继承的 `LLM_API_KEY` 与 `PAOS_SECRET_KEY`。
- 每个测试文件仍保留各自临时库和 `monkeypatch.setattr("app.main.DB_PATH", ...)`，用于处理模块导入顺序。
- 自动 fixture 会拒绝非临时 `DATABASE_PATH` 和真实开发库路径。
- 如无用户明确要求，不要设置 `PAOS_TEST_ALLOW_REAL_DATABASE=1`。

## 6. 备份与恢复注意事项

- 升级或执行未来迁移前，先在应用关闭状态复制 `.db` 文件。应用启动后库为 WAL 模式，**必须同时复制 `-wal` 与 `-shm`**：

```powershell
# 1) 先停应用；2) 三个文件一起复制
Copy-Item data/personal_ai_os.db,data/personal_ai_os.db-wal,data/personal_ai_os.db-shm <备份目录>
# 或运行中取一致快照（无需停服，生成单文件、已 checkpoint 的副本）
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/personal_ai_os.db'); c.execute(\"VACUUM INTO '<备份目录>/snapshot.db'\"); c.close()"
```

  只复制 `.db` 而漏掉 `-wal` 会丢掉尚未 checkpoint 的事务，这是当前最容易犯的备份错误。
- 复制数据库不等于安全复制凭据：connector token 的 Fernet key 默认在 `data/secret.key` 或环境变量中；没有 key 时无法解密已有凭据。
- 恢复前确认文件来自可信来源，并先用临时副本启动验证，不直接覆盖正在使用的开发库。


## 7. C-C-2c 自动调度的数据安全语义

- 本批没有新增或修改表结构；`reminder_scheduler_runs` 与 `reminder_scheduler_leases` 已由前序批次提供。
- 自动 run 使用 `trigger_type='scheduled'` 与 `scheduled:<时间桶>` run key；同桶重放返回已有 run，不自动补齐所有错过的历史桶。
- shutdown 超时后旧线程可能仍存活到当前 SQLite 调用返回；lease TTL 过期并被新 owner 接管后，旧 fencing token 的 guarded write 会被拒绝。
- 异常退出遗留的 `running` run 不立即恢复，超过 5 分钟后由后续持锁 tick 标记为 `abandoned`。
- C-C-2c 所有自动调度测试均使用系统 Temp 下的 SQLite 文件；真实开发库未初始化、迁移、lease 操作或写入。
