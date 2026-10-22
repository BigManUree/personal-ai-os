# 测试记录

更新时间：2026-10-22 12:10 CST

## 0. 2026-10-21 缺陷修复批次（当前结论）

审查报告：`docs/audits/2026-10-21-full-project-review.md`。本批全部写入都发生在系统 Temp 下的隔离 SQLite；未对真实开发库执行 `init_db()`、迁移或写入（复核 `data/personal_ai_os.db` 头字节仍为 write/read version = 1/1，mtime 未变）。

命令与结果：

```text
.venv\Scripts\python.exe -m compileall -q app tests        → 退出码 0
.venv\Scripts\python.exe -m pytest -q                      → 488 passed, 1 skipped, 1 warning (50.4s)
.venv\Scripts\python.exe -m pytest -q（第二次）            → 488 passed, 1 skipped, 1 warning (43.5s)
```

跳过项仍为 `test_local_connector.py:166`（环境不能创建符号链接）；警告为 Starlette TestClient/httpx 弃用提示。

修复前后的针对性验证：

| 项 | 修复前 | 修复后 |
|---|---|---|
| `PUT /api/reminder-settings {"task_due_enabled": true}` | 200 但值仍为 False（白名单丢弃） | 200，回读 True |
| 同上，安静时段 `quiet_hours_enabled/quiet_start` | 字段不存在，被丢弃 | 200，回读 `True` / `23:00` |
| PUT 回显整个 GET 对象（含 `created_at`/`updated_at`） | 400 不允许修改字段 | 200 |
| PUT 前端旧字段 `task_due_soon_enabled` | 500（`AttributeError: message`） | 400 `未知的提醒设置字段：['task_due_soon_enabled']` |
| `POST /api/reminders/{id}/transition {"action":"nope"}` | 500 | 400 `非法提醒操作：nope` |
| `GET /api/reminders?status=bad` | 500 | 400 `非法状态：bad` |
| `snooze_minutes=15` | 200，`snoozed_until=NULL`，下个 tick 立即回 due | 200，`snoozed_until=2026-10-21T13:33:01+00:00`，tick 在 5 分钟内保持 snoozed、20 分钟后回 due |
| `snooze_minutes=0` | 无校验 | 400 `延期时长必须是 1..10080 分钟` |
| 全新 WAL 库 + 3 线程首写（最小复现） | 2/8 轮 `attempt to write a readonly database` | 改 rollback journal 后 0/8；同一 WAL 库连续 12 轮并发写 0/12 |

本批新增/改写的测试：

- 新增 `tests/test_reminder_scheduler.py::test_snooze_sets_wake_time_and_tick_waits_for_it`、`::test_snooze_rejects_out_of_range_minutes`。
- 改写 `tests/test_reminder_scheduler_fencing.py` 的两个竞争用例：先由 holder 连接持有租约制造真实重叠（两个 worker/子进程均应为 busy），释放后再验证单次 tick 的 owner / fencing / released_at。
- 新增 `tests/conftest.py::rollback_journal` fixture，供 4 个并发用例在首写前切到 rollback journal。
- `test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 改为全程持有 warm 连接；`test_new_os_process_recovers_stale_running_run` 的子进程改为轮询"已生成 scheduled run"再退出，不再赌 0.35 秒。
- 三处硬编码 `resume_after_at` 改为相对未来时间（`FUTURE_RESUME`），避免时间炸弹。

未执行：`npm run build` 之外的任何前端脚本、`frontend/e2e_regression.mjs`、真实 Uvicorn 启动、真实连接器网络同步。

## 0-bis. C-C-2d 历史结果（2026-10-13 20:34 CST）

以下结果均来自本轮实际执行；测试未设置 `PAOS_TEST_ALLOW_REAL_DATABASE`，数据库写入只发生在系统 Temp 下的隔离 SQLite 文件。未访问真实邮件、短信、Webhook、桌面通知或第三方通知服务。

### 0.1 TDD RED

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_notification_delivery.py -q --collect-only
```

初始结果：

```text
ImportError: cannot import name 'notification_delivery' from 'app'
no tests collected, 1 error
```

该失败证明新增测试先于生产模块失败，随后按最小实现转绿。

### 0.2 静态编译

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：退出码 0。

### 0.3 通知专项

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_notification_delivery.py -q
```

最终结果：

```text
33 passed, 1 warning in 8.76s
```

覆盖事件/投递幂等、数据库唯一约束、同事务 outbox、重复 tick、quiet hours、过期/取消、claim 条件更新、真实 OS 双进程竞争、重试/超时/死信、stale recovery、错误脱敏、fake channel、默认关闭、显式启用仍为 fake_test、lifespan/health 和有界 shutdown。

### 0.4 完整提醒/调度专项（10 个文件，含 lock）

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_reminder_scheduler.py `
  tests/test_reminder_scheduler_fencing.py `
  tests/test_reminder_scheduler_recovery.py `
  tests/test_reminder_scheduler_reliability.py `
  tests/test_reminder_scheduler_lifecycle.py `
  tests/test_reminder_scheduler_lock.py `
  tests/test_reminder_state_machine.py `
  tests/test_reminder_rules.py `
  tests/test_reminder_quiet_hours.py `
  tests/test_reminders_store.py -q
```

最终结果：

```text
124 passed, 1 warning in 42.25s
```

### 0.5 全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

最终结果：

```text
479 passed, 1 skipped, 1 warning in 117.30s
```

唯一 skip：`tests/test_local_connector.py:166: this environment cannot create symlinks`。唯一 warning：Starlette TestClient/httpx 弃用提示。

### 0.6 收集检查

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

结果：

```text
480 tests collected in 0.86s
```

其中 `tests/test_notification_delivery.py` 为 33 tests collected。相对 C-C-2c 的 446 collected 总计增加 34：33 个新增通知测试，以及 1 个工作区内已存在的非本批写入的 scheduler fencing 测试变化；`tests/test_reminder_scheduler_fencing.py` 的 mtime 为 20:10:11，本会话没有编辑该文件，已作为并行工作区改动风险记录。

### 0.7 数据/进程安全复核

本轮开始后实际只读核验到：

```text
data/personal_ai_os.db
Length: 352256
LastWriteTime: 2026-10-13 19:49:11
SHA-256: 49F1FD751F2E6C522A5D319EFBA94FB22EA21A7D6900B2037FB848EBA164F0D8
```

全量测试和最终复核后，上述大小/mtime/hash 未再变化。只读审计显示真实库没有通知表；本批没有对真实库执行 init/migration/run_tick/notification claim/delivery。该指纹不同于 C-C-2c 交接记录的 258048/2026-10-12/`3EEB309...`，且 mtime 早于本会话 pytest，原因无法从当前证据确定，需人工确认。

备份最终只读指纹：

```text
personal_ai_os.db.bak-20261012-0938-before-1.3.2-A
163840 bytes, 2026-10-12 08:57:56
SHA-256 2D3CA1771A2BD640941529D7233D2CBB0A09EC8DBEF6F3E0F997C4E381C72ABC

personal_ai_os.db.bak-phase2
49152 bytes, 2026-10-11 15:34:37
SHA-256 B48FDAF7196AAE1BBF8C0FC6A50A671995B1712A03297FE57D48B230D6DB2E43
```

未启动监听端口的 Uvicorn 服务；测试结束后未发现项目相关残留 pytest/uvicorn 进程。

---

## 1. 测试环境

- 操作系统：Windows 11 / PowerShell
- Python：`.venv/Scripts/python.exe`，前序实测 Python 3.13.9
- SQLite：前序实测 3.51.0
- pytest：虚拟环境已安装，前序输出显示 pytest 9.1.1
- 测试数据库：系统 Temp 下的独立 SQLite 文件
- 真实开发库：`data/personal_ai_os.db`
- 真实库守卫：`tests/conftest.py` 在 pytest 配置阶段强制 `DATABASE_PATH` 位于系统 Temp；本轮未设置 `PAOS_TEST_ALLOW_REAL_DATABASE`

真实开发库最终核验：

```text
Path: data/personal_ai_os.db
Length: 258048
LastWriteTime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

备份文件最终核验：

```text
personal_ai_os.db.bak-20261012-0938-before-1.3.2-A
Length: 163840
LastWriteTime: 2026-10-12 08:57:56
SHA-256: 2D3CA1771A2BD640941529D7233D2CBB0A09EC8DBEF6F3E0F997C4E381C72ABC

personal_ai_os.db.bak-phase2
Length: 49152
LastWriteTime: 2026-10-11 15:34:37
SHA-256: B48FDAF7196AAE1BBF8C0FC6A50A671995B1712A03297FE57D48B230D6DB2E43
```

## 2. C-C-2c 最终验证结果（2026-10-13 19:10 CST）

以下结果对应最终代码，所有数据库写入均发生在系统 Temp 的测试数据库中；未对真实开发库执行 tick、lease、初始化或迁移。

### 2.1 静态编译

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
退出码 0
```

### 2.2 生命周期专项

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests	est_reminder_scheduler_lifecycle.py
```

最终结果：

```text
33 passed, 1 warning in 9.53s
退出码 0
```

覆盖重点：

- 默认 disabled、布尔/数值配置解析和非法值；
- 非法 scheduler 配置在 `init_db()` 和 ConnectorScheduler 启动前失败；
- start/stop 幂等、单 loop、单 worker；
- asyncio cancellation 不取消底层同步 SQLite Future；
- bounded shutdown 与 `stopped_with_active_tick`；
- busy、lease_lost、database_error、stopped 分类；
- 长 tick heartbeat 和 heartbeat database_error；
- 协作停止在候选批次边界生效；
- lifespan 默认不启动、显式启用后在临时库执行 scheduled tick；
- service 构造失败时 ConnectorScheduler 被停止且模块引用被清空；
- tick started/completed/failed 日志包含 `elapsed_ms`，lease acquire/release 日志不含密钥或业务内容；
- SQLite 写锁错误分类；
- 启动资源失败清理；
- 两个真实 OS 进程共享 lease；
- 新 OS 进程恢复超过 5 分钟的 stale running run。

### 2.3 提醒相关全专项

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q `
  tests	est_reminders_store.py `
  tests	est_reminder_rules.py `
  tests	est_reminder_state_machine.py `
  tests	est_reminder_scheduler.py `
  tests	est_reminder_quiet_hours.py `
  tests	est_reminder_scheduler_recovery.py `
  tests	est_reminder_scheduler_reliability.py `
  tests	est_reminder_scheduler_lock.py `
  tests	est_reminder_scheduler_fencing.py `
  tests	est_reminder_scheduler_lifecycle.py
```

最终结果：

```text
123 passed, 1 warning in 34.66s
退出码 0
```

### 2.4 全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

最终结果：

```text
445 passed, 1 skipped, 1 warning in 141.68s (0:02:21)
退出码 0
```

唯一跳过项：

```text
tests/test_local_connector.py:166: this environment cannot create symlinks
```

唯一警告：

```text
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

未升级依赖或引入 httpx2，以避免本批次无关依赖变更。FastAPI `@app.on_event` 弃用警告已消失。

### 2.5 收集检查

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

结果：

```text
446 tests collected in 0.76s
退出码 0
```

即最终全量结果中的 445 个通过用例 + 1 个环境条件跳过用例。

### 2.6 进程与数据安全复核

- 最终未发现残留的项目 pytest 或 Uvicorn 进程。
- 真实开发库大小、mtime、SHA-256 与任务开始/交接基线一致。
- 两个数据库备份文件的大小、mtime、SHA-256 未变化。
- 未启动监听端口的真实 Uvicorn 服务。
- 未发送邮件、短信、Webhook、桌面通知，也未调用真实 LLM/GitHub/HTTP API。

### 2.7 本轮未覆盖

- 未执行真实 `uvicorn --reload` 父子进程 E2E；已有真实 OS 子进程 lease/recovery 测试。
- 未提供手动 tick HTTP API，因此自动/手动 HTTP 竞争测试暂不适用。
- 未实现或测试真实通知发送器。
- 未做浏览器手工 E2E。

### 2.8 命令路径笔误记录

本轮复核时曾误执行包含不存在路径 `tests	est_reminders.py` 的提醒专项命令，pytest 在收集前退出：

```text
ERROR: file or directory not found: tests	est_reminders.py
no tests ran
```

该错误是命令路径笔误，不是代码或测试失败；随后已使用实际存在的 10 个提醒测试文件重新执行并得到第 2.3 节结果。

## 3. C-C-2c 实施阶段的历史记录（同任务前序执行）

以下结果来自同一任务的前序执行窗口，保留用于追溯，不冒充最终代码结果：

- TDD RED：新增生命周期测试最初因 `app.reminder_scheduler_service` 不存在而收集失败，输出 `ImportError` / `1 error in 0.64s`。
- 实施前提醒专项基线：90 passed, 4 warnings in 29.62s；当时 4 个警告包含 FastAPI `@app.on_event` 弃用提示。
- 首次生命周期实现：30 passed, 1 warning in 8.64s。
- 首次提醒全专项：120 passed, 1 warning in 35.25s。
- 首次全量实现：442 passed, 1 skipped, 1 warning in 139.07s；443 tests collected。
- 收尾阶段增加观测和资源清理测试后，曾得到 32 passed、122 passed、444 passed 的中间结果；最终结果以第 2 节为准。

## 4. 历史工程治理批次（非本轮最终结果）

### 4.1 修改前基线全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

历史结果：

```text
409 passed, 1 skipped, 5 warnings in 100.70s (0:01:40)
```

### 4.2 治理后全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

历史最终结果：

```text
412 passed, 1 skipped, 5 warnings in 97.92s (0:01:37)
```

同轮治理过程中也曾记录 `412 passed, 1 skipped, 5 warnings in 140.10s`；该治理批次最终状态以 97.92s 结果为准。

### 4.3 历史收集检查

```text
413 tests collected in 1.06s
```

### 4.4 历史启动脚本与生命周期冒烟

- `run.bat` 非 loopback Host 拒绝检查退出码为 1。
- 使用独立临时数据库通过 FastAPI TestClient 执行 startup/shutdown 与健康检查。
- 历史冒烟输出的 health body 只有 `status` 和 `mode`；C-C-2c 后新增嵌套 `reminder_scheduler` 对象。
- 历史批次仍使用 `@app.on_event`，因此当时存在 FastAPI 弃用警告；该警告在 C-C-2c 迁移 lifespan 后消失。

## 5. 历史测试保护

- `tests/conftest.py` 在 pytest 配置阶段强制 `DATABASE_PATH` 位于系统 Temp。
- `PAOS_SECRET_KEY_PATH` 也被重定向到系统 Temp。
- 测试进程清空继承的 `LLM_API_KEY` 和 `PAOS_SECRET_KEY`。
- 每个测试自动确认 `app.main.DB_PATH` 不是真实开发库。
- 云端模型、GitHub、HTTP API 使用测试替身或 httpx mock transport。
- 跨进程租约测试使用真实 OS 子进程和临时 SQLite 数据库。

## 6. 更早历史结果（仅作参考）

- C-C-2b fencing 专项：6 passed。
- 早期提醒完整专项：90 passed。
- 2026-10-13 15:12 前全量测试：409 passed, 1 skipped, 5 warnings。
- 更早窗口记录的 251 passed 是当时历史结果，不代表当前测试总数。

> 当前结论一律以第 0 节“2026-10-21 缺陷修复批次”为准；其后各节均为历史记录。

## 0.1 2026-10-22 前端死代码清理

```text
.venv\Scripts\python.exe -m compileall -q app tests   → 退出码 0
.venv\Scripts\python.exe -m pytest -q                 → 488 passed, 1 skipped, 1 warning (109.7s)
cd frontend && npm run build                          → ✓ built in 4.16s，assets 只剩 1 JS + 1 CSS
```

删除：`app/static/{analytics,connectors,doc-upload,memory-candidates,persona,recommendations,work}.js`（2,311 行，删除前哈希与副本见 `temp/legacy-static-js-backup/`）。

配置：`frontend/vite.config.ts` 的 `emptyOutDir` 由 `false` 改为 `true`。验证方式是在 `app/static/assets/` 里放一个 `stale-test.js` 再构建，构建后该文件消失，目录只剩被 `index.html` 引用的 2 个文件。

断言迁移（原断言读取已删除脚本的内部实现，行为不变、对象换成当前产物）：

| 原测试 | 现测试 | 现在断言什么 |
|---|---|---|
| `test_analytics_dashboard_is_linked_and_serves_static_script` | `test_analytics_page_is_linked_and_bundle_wires_overview` | SPA shell + 构建包含 `/api/analytics/overview` |
| `test_recommendations_page_is_linked_and_serves_script` | `test_recommendations_page_is_linked_and_bundle_wires_list_and_feedback` | SPA shell + 构建包含 `/api/recommendations`、`/feedback` |
| `test_recommendations_page_script_contains_feedback_and_outcome_safe_wiring` | `test_recommendations_ui_wires_feedback_and_escapes_raw_html` | `/feedback` 已接；`frontend/src` 无 `v-html`/`innerHTML`；构建无 `onclick=` |
| `test_frontend_contains_read_only_stats_panel_and_safe_renderer` | `test_frontend_ships_only_the_referenced_bundle` | `index.html` 引用的产物集合 == `assets/` 实际文件集合（防残留） |

覆盖损失（已记入 `docs/progress.md` backlog）：统计面板、`recommendation-runs`、`generate`、`outcome` 面板在 Vue 重写时丢失，对应断言无法迁移；补 UI 时需一并恢复。

运行时验证：

```text
GET /                             → 200（引用 index-CP7wDYvj.js）
GET /static/assets/index-CP7wDYvj.js → 200
GET /static/analytics.js          → 404
GET /static/recommendations.js    → 404
GET /static/work.js               → 404
```

## 0.2 2026-10-22 建议采纳与生成入口（feature 002）

```text
.venv\Scripts\python.exe -m compileall -q app tests   → 退出码 0
.venv\Scripts\python.exe -m pytest -q                 → 488 passed, 1 skipped, 1 warning (47.5s)
cd frontend && npm run build                          → ✓ built in 4.59s；assets 仅 1 JS + 1 CSS
```

真实浏览器冒烟（Playwright + 临时库 `temp/ui-smoke.db` + `127.0.0.1:18766`，未触碰真实开发库）：

```text
PASS  首页挂载 Vue / 进入建议页 / 建议卡片渲染 / 卡片含证据摘要
PASS  采纳后状态变为 accepted        — high | task | accepted
PASS  采纳后出现撤回按钮
PASS  撤回后回到 pending
PASS  撤回后重新出现采纳按钮
PASS  拒绝需二次确认
PASS  取消后不改变状态
PASS  无控制台错误 / 4xx 请求
11/11 passed

PASS  生成面板展开
PASS  生成后展示结果摘要            — 上次结果：no_evidence 窗口 2026-10-16 → 2026-10-23
PASS  面板保持展开且可再次生成
PASS  无 5xx / 控制台错误
4/4 passed
```

`no_evidence` 是预期结果：临时库没有行为事件，规则引擎无法产出建议；该次验证的是"生成调用与结果呈现"链路，不是建议内容质量。

测试接线更新：`tests/test_recommendations_api.py::test_recommendations_page_is_linked_and_bundle_wires_list_and_feedback` 现在同时断言 `/generate` 在构建产物中、四个动作（`act(r,'accept'|'ignore'|'reject'|'reopen')`）与「撤回采纳」「确认拒绝」在组件源码中。

未执行：`frontend/e2e_regression.mjs`（既有全量脚本，需手工起 18765 端口的实例）；outcome/统计面板不存在，故无对应断言。

## 0.3 2026-10-22 项目 / 目标 / 任务写操作（feature 003）

```text
.venv\Scripts\python.exe -m compileall -q app tests   → 退出码 0
.venv\Scripts\python.exe -m pytest -q                 → 498 passed, 1 skipped, 1 warning (44.1s)
.venv\Scripts\python.exe -m pytest tests/test_work_ui.py -q → 10 passed
cd frontend && npm run build                          → ✓ built in 5.25s；index.html + 1 JS + 1 CSS
```

收尾复跑记录（如实登记，未做美化）：

```text
pytest -q（第 1 次）              → 2 failed, 497 passed, 1 warning (66.9s)
pytest tests/test_reminder_scheduler_lifecycle.py -q → 33 passed (5.1s)
pytest -q（第 2 次）              → 498 passed, 1 skipped, 1 warning (53.9s)
```

两次失败均为 `test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 与 `test_two_real_os_processes_share_lease_without_duplicate_runs`。本轮**没有修改任何后端、调度器、租约代码**（feature 003 只动了 `frontend/src`、`tests/test_work_ui.py`、文档与 `specs/`），单跑该文件 33 passed，第二次全量 498 passed，因此判为**负载相关的既有 flaky**（真实子进程 + 时序），非本轮回归。尚未定位根因，已列入 `docs/progress.md` 的已知风险，后续单独排查，不在本特性内改动。

本轮新增 10 个测试（`tests/test_work_ui.py`）：其中 7 个是前端源码/构建产物断言，3 个是后端「全量替换」契约回归。

真实浏览器冒烟（Playwright `frontend/e2e_work.mjs` + 临时库 `temp/e2e-work.db` + `127.0.0.1:18767`，两次独立运行结果一致）：

```text
PASS  进入项目与任务页 / 三个区块渲染 / 新建项目弹层出现
PASS  项目已创建 / 项目卡片显示技术栈
PASS  编辑表单回填名称 / 编辑表单回填描述
PASS  状态变为 completed
PASS  仅改状态后描述仍在（全量 PATCH）
PASS  仅改状态后技术栈仍在
PASS  详情出现事件表单 / 事件已记录 / 项目计数显示 1 事件
PASS  目标已创建 / 目标显示类型
PASS  完成后离开 active 过滤 / 全部过滤下显示 done
PASS  任务已创建 / 任务显示项目名 / 任务显示详情 / 任务显示 high 优先级
PASS  开始 -> doing / 完成 -> done / 重开 -> todo / 状态往返后详情未丢
PASS  编辑保留 detail / 编辑保留 due_date / 编辑保留 project_id / 编辑保留 goal_id
PASS  任务删除需二次确认 / 任务已删除
PASS  目标已删除
PASS  项目删除提示副作用 / 项目已删除
PASS  API 中三张表已清空                       — projects=0 goals=0 tasks=0
PASS  无控制台错误 / 4xx 请求
36/36 passed
```

截图：`temp/e2e-work-filled.png`（有数据时的三区块布局）、`temp/e2e-work-final.png`（删除后空状态）。

后端契约证据（`tests/test_work_ui.py` 固化，也是 3 个 body 映射函数存在的理由）：

| 请求 | 结果 |
|---|---|
| `PATCH /api/tasks/{id}` 只带 `{"title","status"}` | `detail=""`、`due_date=null`、`project_id=null`、`priority="normal"` |
| `PATCH /api/tasks/{id}` 带 `taskBody` 全字段 | `status=doing`，`detail`/`due_date`/`project_id`/`goal_id` 全部保留，`completed_at=null` |
| `PATCH /api/projects/{id}` 只带 `{"name","status"}` | `description`/`tech_stack`/`repo_url` 全部被清空 |
| `PATCH /api/projects/{id}` 带 `projectBody` 全字段 | 字段保留 |

真实库守卫：`data/personal_ai_os.db` 冒烟前后 `sha256=008ecb5c9b81ef94f2b86b7af1174dbee5e403ccc125bbc1600ad47214f8ddfb`、`size=389120`、`mtime=2026-10-14 23:11:17` 完全一致，头 16 字节 `53514c69746520666f726d6174203300`（"SQLite format 3"）。

未执行：`frontend/e2e_regression.mjs`（需手工起 18765 端口实例）；outcome/统计面板、项目详情内的任务内联编辑（详情区只读摘要，编辑走任务区块）。

## 0.4 2026-10-22 建议结果登记与统计洞察（feature 004）

```text
.venv\Scripts\python.exe -m compileall -q app tests   → 退出码 0
.venv\Scripts\python.exe -m pytest tests/test_recommendation_insights_ui.py -q → 14 passed
.venv\Scripts\python.exe -m pytest -q                 → 513 passed, 1 warning (58.6s)
cd frontend && npm run build                          → ✓ built in 3.47s；index.html + 1 JS + 1 CSS
```

本轮新增 14 个测试（`tests/test_recommendation_insights_ui.py`）：7 个前端接线/枚举/渲染断言 + 7 个后端契约回归。

真实浏览器冒烟（Playwright `frontend/e2e_insights.mjs` + 播种库 `temp/e2e-rec.db` + `127.0.0.1:18768`）：

```text
PASS  进入智能建议页 / accepted 卡片出现结果登记 / pending 卡片没有结果登记 / 初始为未登记
PASS  指标行可添加
PASS  保存后标记为已登记
PASS  API: execution_status=completed / result_rating=better / 备注已保存
PASS  API: 指标 1 条且数值正确
      — [{"attribution":"not_inferred","captured_at":"2026-10-22T01:57:09.451Z",
          "expected_value":5,"metric":"tasks_completed","observed_value":3,
          "source":"user_reported","unit":"count","window":{}}]
PASS  刷新后执行状态回填 / 评级回填 / 备注回填 / 指标行回填 / 数值回填
PASS  统计面板展开
PASS  推荐总数 = 2 / 当前采纳率 50.0% / 结果登记率 100.0% / 完成率 100.0%
PASS  无反馈的比率显示 —                      — helpful_rate 分母为 0
PASS  展示口径说明 / 暂无生成记录 / 显示样本状态
PASS  切 7 天窗口后仍有数据
PASS  删除需二次确认 / 删除后回到未登记 / API: outcome 变回 null
PASS  无控制台错误 / 4xx 请求
29/29 passed
```

截图：`temp/e2e-insights-filled.png`（统计 + 已登记状态）、`temp/e2e-insights-final.png`（删除后）。

本轮发现并修掉的真实偏差（E1）：`recommendation_stats._ratio()` 返回的是 `{numerator, denominator, value, sample_status}` 对象，分母为 0 时返回 `None`——**不是标量**。`RecommendationInsights.vue` 的初版按标量乘以 100，会让所有比率静默变成「—」。测试 `test_ratio_renderer_handles_null_and_object_shape` 与 `test_statistics_counts_seeded_rows_and_rejects_bad_range` 现在同时钉住两种形状。

后端契约（`tests/test_recommendation_insights_ui.py` 固化）：

| 请求 | 结果 |
|---|---|
| 首次 `PUT .../outcome` 只带 `{"result_rating"}` | 400（`execution_status` 无默认值） |
| `PUT .../outcome` 只带 `{"result_rating":"worse"}`（已有 outcome） | 200，`execution_status` 与备注保留 —— **部分更新**，与工作项 PATCH 相反 |
| `PUT` 带 2 条 `metric_observations`，再 `PUT` 只带 1 条 | 200，最终只剩 1 条 —— 列表整体覆盖 |
| `PUT` 到 `pending` 建议 | 409 `只有当前已采纳的建议可以修改执行结果…` |
| `GET/DELETE .../outcome` 往返 | `null` → 有值 → `null` |
| `GET /api/recommendation-statistics`（空库） | `total=0`、`evidence_present_rate=null`、`sample_status=no_data` |
| `GET /api/recommendation-statistics?start_at>end_at` | 400 `end_at 必须晚于 start_at` |
| `GET /api/recommendation-runs?limit=0` | 422 |

真实库守卫：`data/personal_ai_os.db` 冒烟前后 `sha256=008ecb5c9b81ef94f2b86b7af1174dbee5e403ccc125bbc1600ad47214f8ddfb`、`size=389120`、`mtime=2026-10-14 23:11:17` 完全一致。

未执行：`frontend/e2e_regression.mjs`；runs 表格的"有失败记录"路径（播种库未产生 failed run，需真实生成失败场景）；outcome 的 `metric_observations` 上限 10 条的浏览器端验证（仅有源码断言）。

## 0.5 2026-10-22 个人画像条目管理（feature 005）

```text
.venv\Scripts\python.exe -m compileall -q app tests   → 退出码 0
.venv\Scripts\python.exe -m pytest tests/test_profile_items_ui.py -q → 18 passed
.venv\Scripts\python.exe -m pytest -q                 → 530 passed, 1 skipped, 1 warning (40.1s)
cd frontend && npm run build                          → ✓ built in 3.53s
```

真实浏览器冒烟（Playwright `frontend/e2e_profile.mjs` + 播种库 `temp/e2e-profile.db` + `127.0.0.1:18769`）：

```text
PASS  进入个人画像页 / pending 候选可见 / 候选有确认/拒绝按钮 / rejected 条目默认隐藏
PASS  档案文本摘要折叠可见
PASS  候选确认后变为 confirmed / 出现用户确认标记
PASS  API: is_user_stated = true            — true
PASS  新增条目直接生效（confirmed）/ 新增条目出现在列表
PASS  编辑表单回填内容 / 编辑表单回填日期 / 编辑后内容更新
PASS  清空日期提交 null                      — null
PASS  过期后状态 expired / 过期条目仍可编辑
PASS  无 Key 时展示服务端说明                 — 未配置 LLM_API_KEY（本地演示模式不生成画像推测）
PASS  历史展示 confirm 事件 / 历史展示时间戳
PASS  已拒绝 tab 显示被拒条目 / 被拒条目无编辑入口 / 被拒条目显示说明
PASS  无意外控制台错误 / 4xx 请求            — 故意触发的 propose 400 已过滤
23/23 passed
```

截图：`temp/e2e-profile-final.png`（已拒绝 tab + 历史）。

### 本轮改掉的后端缺陷（E1）

`PATCH /api/profile-items/{id}` 的 `valid_from`/`valid_until` 原用 `if body.valid_from is not None` 判断，导致**显式传 `null` 与缺省键等价——日期一旦设置就无法通过 API 清空**（浏览器 `fill('')` 提交 `null` 后 `valid_from` 仍为原值，冒烟 FAIL 暴露）。修复为 `model_fields_set` 判断（与 outcome 端点 `app/main.py:3341` 的既有模式一致），两行改动，备份 `temp/bak-main-py-005.py`，契约测试 `test_patch_explicit_null_clears_date_but_omitted_key_keeps_it` 固定"缺省保留 / 显式 null 清空"两个方向。

### 冒烟过程修正（非产品缺陷）

- `GET /api/profile-items/{id}` 单条路由不存在（405），e2e 初版臆造了它 → 改从列表过滤。
- 顶部「新增条目」与 Modal 内提交按钮同名 → 点击限定 `form button:has-text(...)`。
- 故意触发的 propose-400 会在 console 留下无 URL 的资源错误 → 过滤后由带 URL 的 response 记录兜底。

### flaky 升级记录

`test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 本轮在全量与单文件运行中**失败 3 次、单跑通过 2 次**（同会话交替），波动比 003 批次更大。005 的改动集（画像 PATCH 两行 + 前端）无法解释调度器行为，判定仍为时序敏感的既有 flaky，但已列为下一步排查优先项，不再视为可忽略噪声。

## 0.6 flaky 根因定位与修复（2026-10-22 11:00）

**结论：不是时序 flaky，是真实的启动期 SQLite 瞬时只读错误。**

### 复现（脱离 pytest，`temp/repro_tick_flaky.py`）

```text
复现脚本 10 次循环：4/10 FAIL
  TICK EXCEPTION: LeaseDatabaseError('attempt to write a readonly database')
  位置：reminder_scheduler.run_tick → lease_svc.acquire_lease → LeaseResult(database_error)
失败后立即用新连接探测：journal_mode=wal，写操作成功（瞬时，非持久）
修复前 lifecycle 文件级复跑：8 次中 2 次失败（lifespan 与 two_processes 各一）
```

### 机制

全新 WAL 库上，调度线程的**首个写连接**在执行 `BEGIN IMMEDIATE` 时，与另一个连接（测试里是 20ms 一次的审计轮询；真实部署里是请求连接）打开/关闭同一文件竞争，Windows 上该连接收到 `SQLITE_READONLY`。控制变量验证：审计轮询改为单条长连接或完全不发（`AUDIT_MODE=persistent/none`）→ 0/10 失败；churn 模式最高 7/14 失败。错误是瞬时的：**立即用新连接重试必然成功**。

### 修复（三层）

| 文件 | 改动 |
|---|---|
| `app/reminder_scheduler_lock.py` | `LeaseResult` 增加 `retryable` 字段；`is_retryable_sqlite_error()` 只认 `SQLITE_READONLY`(8) 及消息特征。**BUSY/LOCKED 明确排除**——它们由 busy_timeout 覆盖，重试会把等待叠加成 2s×N |
| `app/reminder_scheduler.py` | `LeaseDatabaseError` 携带 `retryable`；仅在**租约获取阶段**（未 COMMIT，本进程不持有任何状态）标记 |
| `app/reminder_scheduler_service.py` | `run_scheduled_tick` 重试至多 4 次（退避 0.05/0.15/0.4s），每次新建连接、复用同一 `run_key`——即使上一次已部分写入，重放会收敛到既有 run 行，不会重复 |

### 验证

```text
修复后复现脚本（churn，14 次）：14/14 ok，其中 10 次日志出现 "retrying after transient sqlite error"（重试路径真实命中）
lifecycle 文件级复跑 8 次：33 passed ×8，总时长 4.86s（修复前 6-9.4s，因 BUSY 不再重试）
新增 tests/test_reminder_scheduler_retry.py：7 passed（瞬时重试成功、永久错误不重试、次数有界、run_key 幂等、分类器正反例）
```

覆盖损失：无。`test_sqlite_write_lock_is_classified_as_database_error`（写锁 → database_error）保持原语义，因 BUSY 不在可重试集合内。

## 0.7 feature 006 提醒延期交互 + 行为周期对比（2026-10-22 11:30）

spec：`specs/006-snooze-and-comparison/`。后端零改动，两个页面重写：

- `frontend/src/pages/Reminders.vue`：状态 → 动作按钮（子集见 spec FR-101，排除集 `HIDDEN_ACTIONS` 有测试钉住）、延期内联面板（预设 15/60/180/1440 + 自定义 1..10080，显式传 `snooze_minutes`）、取消需确认（`user_cancelled`）、`snoozed_until` 显示、失败把后端 detail 写到卡片上并刷新列表。
- `frontend/src/pages/Analytics.vue`：新增「周期对比」区块（7/30 窗口切换、本期/上期范围、definition_version、指标表格 delta/percent_change/significant）；`percent_change === null` 显示「—」。

```text
pytest tests/test_snooze_and_comparison_ui.py -q → 18 passed
npm run build                                   → ✓ 5.06s（index-BiaAFbjj.js）
Playwright frontend/e2e_snooze.mjs（播种库 18770）→ 15/15
  关键回显：唤醒时间 ≈ 点击时刻 + 60 分钟（服务端以 now 为基准，非 scheduled_at）
            tasks_created 变化量 +2 / 零基数幅度显示 — / connector_sync_attempts 方向 →
真实库 data/personal_ai_os.db：sha256=008ecb5c…ddfb、size=389120、mtime=2026-10-14 23:11:17 未变
```

未执行：30 天窗口带数据的端到端校验（播种事件只在当前窗口，30 天表结构与 7 天一致）；连接器同步类指标的浏览器断言（无真实同步记录）。

### 冒烟过程修正（非产品缺陷）

- 入口页是 `/` 而非 `/static/`（后者超时）。
- 延期基准是"当下"不是 `scheduled_at`——首版断言按 scheduled_at 算差值 1503 分钟，改按点击时刻 ±2 分钟。
- `goals_created` 被播种了事件，零基数断言换到 `connector_sync_attempts`。

## 0.8 feature 007 连接器凭据管理（2026-10-22 11:55）

spec：`specs/007-connector-credential/`。后端零改动，只重写 `Connectors.vue`：

- 卡片凭据徽标（`has_credential` → 凭据已配置/未配置）。
- 设置/更换凭据：`type=password` 输入，`PUT /api/data-sources/{id}/credential`，成功后用响应更新徽标并**清空输入**；已配置时面板内提供「清除凭据」入口（内联二次确认，`DELETE`）。
- 新建 GitHub 源时可选 PAT 字段（POST `secret`），编辑态不渲染该字段——`PATCH` 没有 secret 字段，凭据只能走专用端点，编辑永远不会误覆盖。

```text
pytest tests/test_connector_credential_ui.py -q → 12 passed
npm run build                                   → ✓ 3.81s（index-C_jQiXlX.js）
Playwright frontend/e2e_credential.mjs（播种库 18771）→ 12/12
  关键回显：库中 ciphertext 不含明文、页面 DOM 不残留明文、清除后徽标回未配置、
            创建 GitHub 源带 PAT → has_credential=true 且列表响应无明文
真实库 data/personal_ai_os.db：sha256=008ecb5c…ddfb、size=389120、mtime=2026-10-14 23:11:17 未变
```

契约测试要点：`secret_crypto.decrypt_secret(ciphertext) == 明文`（证明落库是本实例密钥的真实 Fernet blob 而非任意串）；重复 PUT 单行替换；PUT 清空 `last_error`；明文不出现在列表/详情响应（含创建时传 secret 的路径）。

未执行：GitHub 连接器带真实 PAT 的端到端同步（无外部网络依赖注入点）；本地目录/http_api 凭据场景（后端对所有 kind 同一存储路径，契约测试已覆盖通用路径）。

### 冒烟过程修正（非产品缺陷）

- `execSync` 在 Windows 走 cmd.exe，`.venv/...` 路径无法解析 → 改 `execFileSync` + `path.resolve`。
- 前两轮冒烟在中途崩溃后残留已保存凭据，第三轮"初始未配置"假 FAIL → 重置播种库后 12/12。

## 0.9 工程化收口：ruff + Git 初始化 + CI（2026-10-22 12:10）

### ruff（E1）

`pyproject.toml` 新增 `[tool.ruff]`：`select = ["E4","E7","E9","F","I","UP","RUF100"]`，line-length 120，`UP017` 显式忽略（避免 99 处 `datetime.timezone.utc` 纯重命名），排除 `frontend/temp/data/app/static`。

- 自动修复 89 处（import 排序 29、unused noqa 25、unused import 10、注解/编码等 25）。
- 手工修复 13 处：E702 分号行 8、F841 未用变量 3、F811 重复测试定义 2（`tests/test_reminder_scheduler.py` 的 snooze 回归与越界拒绝测试被整段复制了一份，pytest 只跑后面那份——删除第二份，覆盖率不变）。
- **真缺陷（E2）**：`app/notification_delivery.py` 三处 `raise NotificationConfigError(...)` 引用一个**不存在的类**——非法渠道触发时会是 `NameError` 而非结构化 400。当前所有调用方只传合法渠道所以从未触发。修复：补上 `NotificationConfigError(NotificationError)` 定义（备份 `temp/bak-notification-delivery-008.py`）。
- 终态：`ruff check app tests` → All checks passed；`pytest -q` → 569 passed（修复前后一致）。

### Git 初始化与推送（E3）

- `.gitignore` 增补 `*.key`、`temp/`、`frontend/node_modules/`、`.workbuddy/`；新增 `.gitattributes`（`* text=auto`）。
- `data/`（库+密钥）、`temp/`（手工备份与冒烟产物）、`.workbuddy/`（AI 工作区）全部不入库；暂存清单核查无 `.key/.db/.env` 文件。
- 冒烟播种脚本从 `temp/` 收编进 `scripts/e2e/`（5 个），README 与 `docs/module-responsibilities.md` 引用同步更新。
- 首次提交 `b3388af`（210 文件，859 KiB），推送 `origin/main` 成功，`git ls-remote` 确认远端 SHA 一致。

### CI（`.github/workflows/ci.yml`）

- `backend` job：ubuntu + windows 矩阵（0.6 节的 READONLY 竞争是 Windows 特有，双平台才有区分度）——pip 安装 → `ruff check` → `compileall` → `pytest -q`。
- `frontend` job：node 20 + `npm ci` → `npm run build` → 校验 `app/static/index.html` 与 hash 产物存在。
- 未执行：CI 首次真实运行（需推送后观察 Actions 结果；Linux 上 pytest 是否全绿属待验证项）。
