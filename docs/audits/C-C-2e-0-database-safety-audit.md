# C-C-2e-0：真实开发数据库变更来源调查与只读安全审计

- 审计时间：2026-10-13 20:54:18–21:20:32 CST
- 项目根目录：`D:\develop\VibeCodingProjects\personal-ai-os-mvp`
- 审计方式：文件元数据/SHA-256、SQLite 只读 URI、静态代码/测试阅读、本地会话与命令痕迹检索
- 审计边界：未修改真实数据库或备份；未启动 FastAPI/Uvicorn；未调用 `init_db()`、`run_tick()`、lease、通知 claim/delivery；未运行 pytest；未修改生产代码或测试代码；未读取或输出 `.env` 值。

## 1. 总体结论

1. 当前真实开发库通过只读完整性检查：
   - `PRAGMA integrity_check` = `ok`；
   - `PRAGMA foreign_key_check` 无结果；
   - 未发现视图、触发器、WAL、SHM 或 journal 残留。
2. 19:49:11 的具体写入进程/人工操作**未能最终确认**；项目不是 Git 仓库，也没有命令日志能直接还原操作者。
3. 数据库内容形态高度一致于“C-C-2d 代码合入前、C-C-2c 版本应用启动时执行过一次 `init_db()` 幂等建表/种子”：
   - 新增 `reminder_settings` 默认行，时间恰为 `2026-10-13T11:49:11Z`（CST 19:49:11）；
   - 新增提醒、建议、行为分析等 8 张表；
   - 有 `reminder_scheduler_runs` 但没有 `reminder_scheduler_leases`，符合“建表但未执行 tick”；
   - 没有 C-C-2d 的两张通知表，符合当前 19:49 时尚未合入通知 schema 的 C-C-2c 代码形态。
   以上是基于证据的高置信推断，不等同于已查明具体进程。
4. 未发现 19:49 后新增业务数据：`reminders=0`、`reminder_scheduler_runs=0`，现有业务内容时间上限仍为 2026-10-12 16:45:50 CST；没有证据显示自动调度或通知投递执行过。
5. 当前库没有损坏证据，且比两个备份包含更多 10 月 12 日业务数据。**不建议未经人工确认直接用旧备份覆盖当前库**，否则有明确丢数风险。
6. `tests/test_reminder_scheduler_fencing.py` 的 20:10:11 修改来源也未能从本地 Codex 会话日志中确认；静态审查未发现连接真实开发库的路径，测试均指向 `tmp_path` 临时库。

## 2. 文件指纹与 SQLite 附属文件

### 2.1 审计开始时指纹

| 文件 | 大小 bytes | CreationTime CST | LastWriteTime CST | SHA-256 |
|---|---:|---|---|---|
| `data/personal_ai_os.db` | 352,256 | 2026-10-10 18:04:26.185 | 2026-10-13 19:49:11.445 | `49F1FD751F2E6C522A5D319EFBA94FB22EA21A7D6900B2037FB848EBA164F0D8` |
| `data/personal_ai_os.db.bak-20261012-0938-before-1.3.2-A` | 163,840 | 2026-10-12 09:38:37.189 | 2026-10-12 08:57:56.967 | `2D3CA1771A2BD640941529D7233D2CBB0A09EC8DBEF6F3E0F997C4E381C72ABC` |
| `data/personal_ai_os.db.bak-phase2` | 49,152 | 2026-10-11 17:32:10.921 | 2026-10-11 15:34:37.196 | `B48FDAF7196AAE1BBF8C0FC6A50A671995B1712A03297FE57D48B230D6DB2E43` |

### 2.2 附属文件

审计开始和结束时，`data/` 中均未发现当前库对应的：

- `personal_ai_os.db-wal`
- `personal_ai_os.db-shm`
- `personal_ai_os.db-journal`

三个数据库的 `journal_mode` 均为 `delete`。因不存在 WAL/SHM 文件，只读连接看到的是主库文件本身；不存在未检查 WAL 内容的问题。

## 3. SQLite 只读完整性与对象清单

只读连接形式：

```python
sqlite3.connect(Path(...).resolve().as_uri() + "?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
```

SQLite 运行库版本：`3.51.0`。

| 项目 | 当前库 | 备份 A | phase2 备份 |
|---|---:|---:|---:|
| integrity_check | `ok` | `ok` | `ok` |
| foreign_key_check | 0 行 | 0 行 | 0 行 |
| user_version | 0 | 0 | 0 |
| application_id | 0 | 0 | 0 |
| page_size | 4,096 | 4,096 | 4,096 |
| page_count | 86 | 40 | 12 |
| freelist_count | 0 | 0 | 0 |
| journal_mode | delete | delete | delete |
| encoding | UTF-8 | UTF-8 | UTF-8 |
| 表数量 | 27 | 19 | 6 |
| 索引数量 | 35 | 10 | 0 |
| 视图 | 0 | 0 | 0 |
| 触发器 | 0 | 0 | 0 |

## 4. 表与行数对比

### 4.1 当前库每表行数

| 表 | 行数 |
|---|---:|
| `approvals` | 16 |
| `behavior_events` | 16 |
| `connector_credentials` | 0 |
| `conversations` | 50 |
| `data_sources` | 0 |
| `document_chunks` | 16 |
| `documents` | 16 |
| `goals` | 0 |
| `memories` | 18 |
| `memory_candidates` | 0 |
| `memory_revisions` | 32 |
| `messages` | 93 |
| `profile` | 1 |
| `profile_item_revisions` | 0 |
| `profile_items` | 0 |
| `project_events` | 0 |
| `projects` | 0 |
| `recommendation_feedback` | 0 |
| `recommendation_outcomes` | 0 |
| `recommendation_runs` | 0 |
| `recommendations` | 0 |
| `reminder_scheduler_runs` | 0 |
| `reminder_settings` | 1 |
| `reminders` | 0 |
| `source_items` | 0 |
| `sync_logs` | 0 |
| `tasks` | 0 |

### 4.2 备份行数

| 表 | 备份 A | phase2 备份 |
|---|---:|---:|
| `approvals` | 0 | 0 |
| `conversations` | 33 | 33 |
| `documents` | 0 | 0 |
| `memories` | 2 | 2 |
| `messages` | 60 | 60 |
| `profile` | 1 | 1 |
| `document_chunks` | 0 | — |
| `memory_revisions` | 0 | — |
| 其他备份 A 中的业务表 | 0 | — |

### 4.3 当前库相对备份新增的表

相对备份 A，当前库新增 8 张表：

- `behavior_events`
- `recommendation_feedback`
- `recommendation_outcomes`
- `recommendation_runs`
- `recommendations`
- `reminder_scheduler_runs`
- `reminder_settings`
- `reminders`

相对 phase2 备份，当前库新增 21 张表，除上述 8 张外，还包括 connector、document、memory revision、profile item、project、task、sync 等表。

### 4.4 共同表 schema 差异

- 当前库 vs 备份 A：共同表没有列定义差异。
- 当前库 vs phase2：
  - `documents` 新增 `file_hash`、`source_type`、`status`、`original_filename`、`file_ext`、`byte_size`、`error`、`chunk_count`、`updated_at`；
  - `memories` 新增 `memory_type`、`source_type`、`source_id`、`status`、`valid_from`、`valid_until`、`supersedes_id`。

## 5. 关键业务状态（不含个人正文）

### 5.1 时间范围

- 当前库既有业务表（如 `approvals`、`behavior_events`、`documents`、`memories`、`messages` 等）的最大时间为：`2026-10-12T08:45:50+00:00`，即 CST `2026-10-12 16:45:50`。
- `reminder_settings` 的唯一种子行为：
  - `created_at = 2026-10-13T11:49:11Z`
  - `updated_at = 2026-10-13T11:49:11Z`
  - 所有提醒开关均为 `0`
  - `timezone = Asia/Shanghai`
  - 日复盘时间 `20:00`；周复盘为周一 `09:00`
- `reminders`：0 行。
- `reminder_scheduler_runs`：0 行。
- `reminder_scheduler_leases`：表不存在。
- `reminder_notification_events`：表不存在。
- `reminder_notification_deliveries`：表不存在。

### 5.2 与当前源码的静态表数量对照

静态正则解析，不执行代码：

- `app/main.py::init_db()` 当前定义 27 张 `CREATE TABLE IF NOT EXISTS`；
- `app/notification_delivery.py` 定义 2 张通知表；
- `app/reminder_scheduler_lock.py` 在首次 lease 操作时定义 1 张 `reminder_scheduler_leases`。

当前真实库包含 `app/main.py` 的 27 张表，但缺少两张通知表和 lazy 创建的 lease 表。这说明当前库尚未运行过 C-C-2d 版本的 `init_db()`；其 19:49 形态与 C-C-2d 修改前的 C-C-2c 版本初始化一致。

## 6. 19:49:11 变化来源调查

### 6.1 可确认事实

1. 数据库 mtime 精确为 `2026-10-13 19:49:11.445 CST`，唯一种子行时间为同一时刻的 UTC 值 `11:49:11Z`。
2. 19:47:10 至 19:50:57 之间，当前 Codex rollout 会话没有任何命令执行事件：
   - 最后一条前序命令：19:47:09 读取 `app/reminder_scheduler_service.py`；
   - 下一条命令：19:50:57 之后才恢复。
3. 对本地 5 个 Codex rollout 会话扫描，19:48–19:50 没有任何 `CommandExecution` 或工具调用事件。
4. PSReadLine 历史中未发现该项目的 `uvicorn app.main:app`、`run.bat`、`init_db()`、`run_tick()` 或 pytest 命令；仅有与其他项目相符的通用 uvicorn 命令和一条较早的 icacls 命令。
5. PowerShell Operational 日志在 19:40–19:55 未检索到目标关键词；Security 4688 进程创建事件不可用（查询结果为无匹配事件/通道无可用事件）。
6. 审计时端口 8000 无监听；仅发现一个与本项目无关的 Anaconda `pythonw.exe` 后台进程。
7. 19:49 的数据库没有通知表；C-C-2d 生产文件的关键修改时间为 20:02:58，`app/main.py` 接入通知 schema 的修改时间为 20:19:08，均晚于数据库变更。

### 6.2 高置信推断（不是已证实的操作日志）

变更内容高度符合一次 **C-C-2c 版本 `init_db()` 幂等初始化**：

- `CREATE TABLE IF NOT EXISTS` 可在旧库上补齐新表而不删除旧数据；
- `INSERT OR IGNORE INTO reminder_settings ... strftime('%Y-%m-%dT%H:%M:%SZ','now')` 会在 19:49:11 插入全开关关闭的默认设置；
- 有 `reminder_scheduler_runs` 表但没有 lease 表，符合只执行 startup 建表、未执行 `run_tick()`；
- 所有业务数据仍停留在 10 月 12 日，没有提醒、run、lease、通知记录。

可能入口包括人工启动旧版应用、外部终端/IDE 执行初始化、或未被本地 Codex 日志记录的并行工具。缺少进程创建日志，无法确认具体进程、命令行或操作者。

### 6.3 已排除或缺少证据支持的情况

- 没有证据显示本轮审计写入真实库。
- 没有证据显示当前 Codex 会话在 19:49 执行了命令。
- 没有证据显示执行过 `run_tick()`：没有 run/lease 数据。
- 没有证据显示通知 claim/delivery 运行过：通知表尚不存在。
- 不能把文件大小变化本身解释为损坏；`integrity_check` 和 `foreign_key_check` 均通过。
- 不能证明是 C-C-2d 全量 pytest 所致：测试守卫要求临时库，且全量测试发生在更晚；当前库也没有通知表。

## 7. fencing 测试文件调查

### 7.1 文件指纹

- 路径：`tests/test_reminder_scheduler_fencing.py`
- 大小：17,871 bytes
- CreationTime / LastWriteTime：均为 `2026-10-13 20:10:11.311 CST`
- SHA-256：`01F99BD9F55C1D2408DCEAB08F22E84A7D20F864D8728328D6FD58B89EA465F2`
- 相关 pycache：
  - `tests/__pycache__/test_reminder_scheduler_fencing.cpython-313-pytest-9.1.1.pyc`：20:11:46
  - `tests/__pycache__/test_reminder_scheduler_fencing.cpython-313.pyc`：20:15:05

### 7.2 静态内容风险

当前文件包含 busy lease、stale fencing、完整 `run_tick()` OS 子进程竞争、过期接管后旧 owner 不得继续写入等测试。静态审查结果：

- fixture 使用 `tmp_path / "scheduler_fencing.db"`，并 `monkeypatch.setenv("DATABASE_PATH", str(path))`、`monkeypatch.setattr("app.main.DB_PATH", path)`；
- 过期接管 E2E 测试使用 `tmp_path / "expired-takeover.db"`；
- 子进程显式接收临时库路径参数，并独立 `sqlite3.connect(path)`；
- 未发现硬编码 `data/personal_ai_os.db`；
- `init_db()` 和 `run_tick()` 均作用于临时库；
- 子进程测试不访问网络、不发送通知、不读取 `.env` 密钥。

### 7.3 来源未知项

- 20:08:30–20:12:30 的 5 个本地 Codex rollout 会话中，没有发现写入 `tests/test_reminder_scheduler_fencing.py` 的命令。
- 20:15 的提醒回归 pytest 命令确实执行了该文件，但这只能解释 pycache/测试读取，不能解释源文件 20:10:11 的内容变更。
- 工作区不是 Git 仓库，无法用 diff 或 blob 历史确认新增了哪些行。
- 可从注释看出文件包含“P0-1 expired-takeover end-to-end fencing”测试，但无法在没有旧文件副本的情况下证明完整历史差异。

结论：当前文件内容的测试隔离风险低；修改归属和精确历史差异无法确认。

## 8. C-C-2d 通知实现静态安全审查

本节约论均为静态代码阅读结论，本轮未运行应用、未执行通知功能、未做动态测试。

### 8.1 同事务 outbox

- `app/reminder_scheduler.py::_mark_due_one()` 在 `guarded_write(conn, guard, now)` 内：
  1. 调用 `reminders.transition_reminder(..., "mark_due")`；
  2. 仅当新状态为 `due` 时调用 `notification_delivery.create_reminder_due_outbox()`。
- guarded 写入以 `BEGIN IMMEDIATE` 开始，并在提交前复查 lease/fencing；因此 reminder due 状态变更与 event/delivery 插入共享同一 SQLite 事务边界。
- outbox 插入失败会使该事务回滚，提醒状态不会单独提交。

### 8.2 唯一约束与外键

- `reminder_notification_events.event_key TEXT NOT NULL UNIQUE`。
- `reminder_notification_deliveries.delivery_key TEXT NOT NULL UNIQUE`。
- delivery 通过 `event_id` 外键关联 event，并配置 `ON DELETE CASCADE`。
- 表级 `CHECK (channel IN ('fake_test'))` 将渠道限制为 fake test。

### 8.3 幂等领取、重试与死信

- claim 使用单条条件 `UPDATE ... WHERE id=(SELECT ... ) ... RETURNING *`，并在 `BEGIN IMMEDIATE` 事务中提交。
- SQLite 条件更新是跨进程 claim 权威；asyncio/threading 锁只处理同进程并发。
- 状态包括 `pending`、`running`、`retry_waiting`、`succeeded`、`dead_letter`、`cancelled`。
- stale `running` 可恢复为 `retry_waiting`，超过次数则死信。
- 语义是 at-least-once，不宣称 exactly-once。

### 8.4 默认关闭与渠道限制

- `notification_delivery_service.load_config()` 对未设置环境变量返回 `enabled=False`。
- `NotificationDeliveryService.start()` 在 disabled 时直接保持 stopped，不创建循环。
- service 构造函数拒绝名称不是 `fake_test` 的 channel；`process_delivery_batch()` 再次校验。
- `FakeNotificationChannel.send()` 只记录内存调用并返回 fake message id，不执行网络/桌面/邮件/短信 I/O。
- 静态导入和调用路径未发现真实邮件、短信、Webhook、桌面通知或云通知发送器。

### 8.5 lifespan 与健康检查

- 配置在 `init_db()` 前解析；非法 scheduler/notification 配置会阻断启动，不建表、不启动服务。
- 启动顺序：connector → reminder producer → notification consumer。
- 关闭顺序：reminder → notification → connector。
- notification 启动失败时，已启动的 reminder/connector 会在嵌套 `finally` 中清理。
- 静态注意项：`connector.start()` 位于外层 `try/finally` 之前；如果 connector 自身在启动过程中部分成功后抛错，当前片段没有专门的 connector 启动失败回滚。这是既有 connector 生命周期的 P2 健壮性观察，不影响本次“消费者默认关闭、无真实通知”的结论。
- `/api/health` 仅返回模式、调度器状态、通知服务状态、计数和脱敏错误字段；未返回环境变量、API key、token、密码或连接串。

## 9. 风险分级

| 等级 | 问题 | 证据 | 建议 |
|---|---|---|---|
| P1 | 19:49:11 真实开发库写入的操作者/进程未知 | 无 Git；无命令日志；rollout 无该时间点命令；Security 4688 不可用 | 人工确认当时是否启动过旧版应用；确认前不要覆盖/恢复/迁移 |
| P2 | 当前库尚未包含 C-C-2d 通知表 | 表清单无两张 notification 表；当前代码下次 `init_db()` 会幂等创建 | 下次启动前由人工知情确认并可先做文件级备份；不要把“未建表”误报为投递故障 |
| P2 | fencing 测试文件修改来源未知 | mtime 20:10:11；会话日志无写入命令；无 Git diff | 人工确认是否为并行会话补写 P0-1 E2E 用例；当前内容静态隔离安全 |
| P2 | 启动失败回滚对 connector 自身启动异常覆盖不完整 | `connector.start()` 在 `try/finally` 外 | 后续单独做生命周期评审和临时库测试，不与本审计混做 |
| P3 | 历史文档中存在旧表数量/旧指纹段落 | 文档形成于不同阶段 | 后续更新数据库文档时以本审计 27/29/lazy-30 的静态对照为准 |

## 10. 处置建议

1. **建议保留当前 352,256 bytes 数据库，不做恢复。** 它通过完整性检查，包含比两个备份更新的业务数据；旧备份明显早于当前业务状态，直接覆盖会丢数。
2. 在人工确认 19:49 来源前，不启动会连接真实库的应用、不执行迁移/修复/调度。
3. 人工确认后，如接受该 schema 初始化，可在应用未运行时另行制作一份带时间戳的文件级备份；之后首次启动当前 C-C-2d 代码时，`init_db()` 预计只会幂等补齐两张通知表，不会自动消费通知。
4. 不应把本次审计结果解释成真实通知渠道已完成；当前仍只有 `fake_test`。

## 11. 本轮实际执行的只读检查

代表性命令/脚本：

```powershell
.\.venv\Scripts\python.exe --version
Get-ChildItem -LiteralPath data -Force
Get-FileHash data\personal_ai_os.db -Algorithm SHA256
Get-NetTCPConnection -LocalPort 8000
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|uvicorn' }
```

```python
# 通过 stdin 执行的 SQLite 检查：mode=ro + PRAGMA query_only=ON
sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
conn.execute("PRAGMA integrity_check")
conn.execute("PRAGMA foreign_key_check")
conn.execute("SELECT type, name, sql FROM sqlite_master ...")
conn.execute("SELECT COUNT(*) FROM ...")
```

还执行了：

- 静态读取 `AGENTS.md`、README、数据库/测试/决策/通知设计文档；
- 静态读取 `app/main.py`、`app/reminder_scheduler.py`、`app/notification_delivery.py`、`app/notification_delivery_service.py`、`tests/conftest.py`、`tests/test_reminder_scheduler_fencing.py`；
- 只读检索 PSReadLine、PowerShell Operational、Codex rollout 与 Codex SQLite 日志；
- 未运行 pytest、compileall、Uvicorn、HTTP 请求或业务脚本。

## 12. 仍无法证实的事项

- 19:49:11 是哪一个 PID、终端、IDE、人工动作或外部进程触发了旧版初始化。
- 20:10:11 fencing 测试源文件由哪个会话或外部编辑器修改。
- 已不存在独立文件备份的 258,048 bytes 历史基线与当前库之间的逐页二进制差异；当前只能依据表、行数、时间戳和历史指纹记录判断。

## 13. 允许与禁止的后续操作

### 允许

- 继续使用 SQLite `mode=ro` 做只读核查；
- 在人工明确批准后，先复制出一份新的时间戳备份；
- 在临时库中运行 pytest 或生命周期测试；
- 对 connector 启动失败回滚做单独设计和临时库测试。

### 禁止

- 禁止用两个旧备份直接覆盖当前库；
- 禁止删除当前库、WAL/SHM（若未来出现）或任何备份；
- 禁止为了“验证”而启动连接真实库的应用；
- 禁止在真实库执行 `init_db()`、迁移、seed、repair、lease、`run_tick()`、通知 claim/delivery；
- 禁止接入或启用真实邮件、短信、Webhook、桌面或云通知渠道。

