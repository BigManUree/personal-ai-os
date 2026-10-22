# 全项目审查：未完成项与待优化项

审查时间：2026-10-21 21:00 CST
审查方式：静态阅读 + 实机执行（编译、全量 pytest、临时库 API 探测、SQLite 头字节读取）
审查范围：`app/`、`tests/`、`frontend/`、`docs/`、`specs/`、根目录配置

## 0. 本次实际执行的验证

| 验证 | 命令 | 结果 |
|---|---|---|
| 静态编译 | `.venv/Scripts/python.exe -m compileall -q app tests` | 退出码 0（COMPILE_OK） |
| 全量测试 | `.venv/Scripts/python.exe -m pytest -q` | **20 failed, 467 passed, 1 warning, 50.88s** |
| 单点复跑 | 上述 20 条中代表性 6 条 `--tb=line` | 3 类根因（见 E3/E4/E5） |
| 设置接口探测 | 临时库 + `TestClient`，`PUT /api/reminder-settings` | 200 但字段被静默丢弃；UI 字段名返回 500 |
| 错误路径探测 | `POST /api/reminders/1/transition {"action":"nope"}` | **500**（应为 400） |
| 数据库日志模式 | 读取 `data/personal_ai_os.db` 头部字节 18/19 | `1/1` = rollback journal（当前确实非 WAL） |
| 并发写探针 | 自建 WAL / DELETE 两种模式下 6 线程 `BEGIN IMMEDIATE` | 均无错误（说明 E5 不是单纯 WAL 并发问题） |

未执行：`npm run build`、Vite dev server、`frontend/e2e_regression.mjs`、任何真实外部网络调用、任何对真实开发库的写入。

---

## 1. 结论摘要

项目主体（长期记忆、文档检索、画像/建议、连接器、提醒+调度+通知 outbox）设计纪律较好，安全边界（loopback Host 校验、来源校验、密钥 Fernet 加密、临时库守卫、fencing 写入）在同类 MVP 中属于上乘。但当前处于**"后端跑通、前端重写未完、测试转红、文档失真"**的状态，不能按 README 宣称的状态交付。

| 级别 | 问题 | 影响 |
|---|---|---|
| P0 | 提醒设置 API 与领域模型字段名错位，双向失效 | 设置页保存必定 500 或静默无效 |
| P0 | `ReminderError.message` 不存在，3 个路由错误路径抛 `AttributeError` | 所有非法输入变 500，错误信息丢失 |
| P0 | 租约释放用真实墙钟、获取用注入时钟 | 14 条调度测试红；注入时钟的补跑/回放会卡死租约 |
| P0 | `snooze` 不写 `snoozed_until` | 延期后下一次 tick 立刻回弹为 due，功能等同无效 |
| P1 | 前端 Vue 重写只覆盖约一半后端能力 | 项目/任务/目标/画像条目/建议状态机/凭据管理均无 UI |
| P1 | 4 个并发用例 `attempt to write a readonly database` | 未定位；单跑通过/复跑失败，属不稳定套件 |
| P1 | 3 个用例硬编码 2026-10-20 且校验"未来时间" | 时间一到必红，属时间炸弹测试 |
| P1 | 文档仍描述"无构建的原生前端" | README/AGENTS/architecture/module-responsibilities 全部失真 |
| P1 | `main.py` 3,461 行 / 88 路由 / 27 表 / 5 个手写迁移 | 变更成本高，无迁移版本表 |
| P2 | 检索全表扫描 + Python 打分 + 硬编码上限 | 数据量上千后延迟与召回同时恶化 |
| P2 | 无 Git 仓库、无 CI、无 lint/类型检查、无前端测试 | 回归只能靠人手跑 pytest |
| P2 | Spec Kit 只落地 001 基线 | phase 2–5 全部绕过强制工作流 |

---

## 2. P0 缺陷详述

### E1 提醒设置接口与领域模型字段名不一致（双向失效）

- 路由白名单：`app/main.py:1443-1447`
  允许 `daily_review_enabled`、`weekly_review_enabled`、`recommendation_followup_enabled`、**`task_due_soon_enabled`**、**`task_overdue_enabled`**、`task_stalled_enabled`、**`review_time_local`**、`timezone`。
- 领域模型：`app/reminders.py:310-315` `SETTINGS_FIELDS`
  `timezone`、**`task_due_enabled`**、`task_stalled_enabled`、`recommendation_followup_enabled`、`daily_review_enabled`、`weekly_review_enabled`、**`daily_review_at`**、**`weekly_review_weekday`**、**`weekly_review_at`**、**`quiet_hours_enabled`**、**`quiet_start`**、**`quiet_end`**。
- 实测（临时库，`frontend/src/pages/Settings.vue` 提交的 8 个字段）：
  `ReminderError: 不允许修改字段：['review_time_local', 'task_due_soon_enabled', 'task_overdue_enabled']`
- 实测（提交真实字段名）：`PUT {"task_due_enabled": true}` → **200**，随后 `GET` 回来仍是 `false`（被白名单过滤成空 dict，`update_settings({})` 空更新）。
- 前端同样错位：`frontend/src/pages/Settings.vue:7-12` 的 `task_due_soon_enabled` / `task_overdue_enabled` / `review_time_local` 在后端不存在；`quiet_hours_enabled/quiet_start/quiet_end`、`daily_review_at`、`weekly_review_weekday/weekly_review_at` 在前端完全缺失。

修复方向（两处）：

```python
# app/main.py:1441-1453 —— 删除白名单，交给 SettingsUpdate(extra="forbid") 校验
@app.put("/api/reminder-settings")
def update_reminder_settings_api(body: dict[str, Any]) -> dict[str, Any]:
    with db() as conn:
        try:
            reminders.update_settings(conn, body)
            return reminders.get_settings(conn)
        except reminders.ReminderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
```

```ts
// frontend/src/pages/Settings.vue —— 用 GET 返回的全量字段做 v-model，并补齐安静时段
```

### E2 `ReminderError` 没有 `.message`，错误路径抛 `AttributeError` → 500

- 定义：`app/reminders.py:86-90` 只设置 `code` / `status_code`，消息交给 `Exception.args`。
- 引用：`app/main.py:1421`、`1432`、`1453` 三处 `detail=exc.message`。
- 实测：`POST /api/reminders/1/transition {"action":"nope"}` → **500**（`AttributeError: 'ReminderError' object has no attribute 'message'`）；`GET /api/reminders?status=bad` → **500**。
- 影响：所有提醒类接口的参数校验失败都变成 500，前端只拿到"请求失败 (500)"，且服务端日志被 traceback 污染。

修复：三处改为 `detail=str(exc)`；或给 `ReminderError` 增加 `self.message = message`（后者更兼容，但会掩盖同类拼写错误）。

### E3 调度租约时钟混用：获取用注入时间，释放用真实墙钟

- 获取：`app/reminder_scheduler.py:794-797` 传 `now=current_dt`（`run_tick(now=...)` 的入参）→ `app/reminder_scheduler_lock.py:224-226` 以其计算 `expires_at`。
- 释放：`app/reminder_scheduler.py:870-874` 传 `now=lease_svc.utc_now()`（真实时间）→ `lock.py:344-353` 把 `expires_at` 覆盖为真实时刻。
- 后果：任何以历史/未来逻辑时间调用的 tick（测试、补跑、回放）释放后，下一次 `acquire` 用逻辑时间比较 `expires_at <= now` 恒为 False，返回 `STATUS_BUSY` → `LeaseBusyError`；部分路径先被抢走则 `LeaseLostError`。
- 实测：`tests/test_reminder_scheduler.py::test_tick_creates_due_soon_candidate_once_and_marks_it_due` 单跑必红：
  `at rs.run_tick(conn, now=NOW + timedelta(minutes=5), run_key="run-2")` → `LeaseBusyError`。
- 受影响用例（同源，已抽样确认 6 条，推断覆盖其余）：`test_reminder_scheduler.py` 8 条、`test_reminder_scheduler_lifecycle.py` 1 条、`_lock.py` 1 条、`_recovery.py` 1 条、`_reliability.py` 3 条、`test_notification_delivery.py` 2 条。

修复方向：释放时使用同一个逻辑时钟。

```python
# app/reminder_scheduler.py:869-874
released = lease_svc.release_lease(
    conn, owner_token=lease.owner_token, lease_token=lease.lease_token,
    fencing_token=lease.fencing_token,
    now=current_dt,          # 原为 lease_svc.utc_now()
    busy_timeout_ms=LEASE_BUSY_TIMEOUT_MS,
)
```

注意：生产路径里 `run_scheduled_tick` 传的 `now` 本来就是墙钟（`service.py:199`），所以该缺陷在生产被掩盖；但"注入时钟必须自洽"是这套 fencing 设计的前提，测试红只是表象。

### E4 `snooze` 不写 `snoozed_until`，延期立即失效

- `app/reminders.py:733-741` 的 `target_status` 有 `snooze → STATUS_SNOOZED`，但后续 `sets` 分支（`:744-768`）没有任何 `snoozed_until` 赋值；API 也只接受 `action` 与 `cancel_reason`（`app/main.py:1425-1431`）。
- `app/reminder_scheduler.py:400-402`：`ready_at = row["snoozed_until"] if status == "snoozed" else scheduled_at`；`ready_at is None` 时跳过"未到时间"判断，直接 `_mark_due_one`。
- 后果：点"延期"后，下一次 tick 立刻把提醒重新置为 `due`，且 `snoozed_until` 永远为 NULL。
- `TRANSITIONS`（`:50-58`）本身允许 `snooze` 从 `due` 出发，状态机是通的，缺陷纯在字段写入。

修复：`transition_reminder(..., snooze_minutes: int = 10)`，在 `snooze` 分支追加
`sets.append("snoozed_until=?")`、`params.append(iso(ts + timedelta(minutes=snooze_minutes)))`，并在 HTTP body 与前端补上时长选择。

### E5 并发用例 `attempt to write a readonly database`（未定位，标记待查）

- 现象：`tests/test_reminders_store.py::test_database_unique_constraint_and_concurrent_creators` 单跑通过，全量跑失败；`tests/test_reminder_scheduler_reliability.py::test_concurrent_connections_create_same_dedup_key_exactly_once` 单跑即失败。
- 报错：`OperationalError('attempt to write a readonly database')`，出现在 `threading.Barrier` 之后的工作线程里。
- 已排除：自建探针在 `journal_mode=wal` 与 `delete` 下各起 6 线程 `BEGIN IMMEDIATE`，均无错误 → 不是"WAL + 多连接"这么简单的组合。
- 下一步可复现命令：

```powershell
.\.venv\Scripts\python.exe -m pytest "tests/test_reminder_scheduler_reliability.py::test_concurrent_connections_create_same_dedup_key_exactly_once" -q --tb=long -s
```

排查建议：在 `create_reminder_result` 的 `BEGIN IMMEDIATE` / `ROLLBACK` / `SAVEPOINT` 分支加连接状态断言（`conn.in_transaction`），确认是否在"已回滚的连接"上继续写入；同时确认 `tmp_path` 是否被并发清理。

---

## 3. 前端与 API 的断层

### E9 技术栈与文档完全脱节

| 位置 | 文档写的 | 实际 |
|---|---|---|
| `README.md:43` | 原生 HTML/CSS/JS，无前端构建步骤 | Vue 3 + Vite 6 + Tailwind，`frontend/` 需构建 |
| `AGENTS.md`（目录边界） | `app/static/：无构建前端` | `app/static/index.html` 引用 `assets/index-*.js` 构建产物 |
| `docs/architecture.md:7,30` | 无构建步骤，`app/static/` 直接调 API | 同上 |
| `docs/module-responsibilities.md:34` | `static/` 无构建浏览器 UI | 同上 |

### E10 死代码与产物堆积

- 7 个原生脚本共 2,311 行已无任何引用：`analytics.js`、`connectors.js`、`doc-upload.js`、`memory-candidates.js`、`persona.js`、`recommendations.js`、`work.js`。
- `app/static/assets/` 累积 3 个 JS + 2 个 CSS，其中 `index-BG2H5jI2.js`、`index-uneSmSWi.js`、`index-CNkHeUzY.css` 是旧构建残留（`vite.config.ts` 用了 `emptyOutDir: false`）。
- 当前生效产物 `index-BvWh3s6A.js` 已包含最新文案（抽查"调度器和通知服务默认关闭""暂无提醒""知悉"均命中），说明**产物不旧、但目录不干净**。
- `frontend/src/components/` 为空目录。

### E11 后端能力无 UI（粗略口径：约 40 / 88 个路由无前端调用点）

| 能力组 | 后端 | 前端现状 |
|---|---|---|
| 项目/任务/目标 | `projects` 全 CRUD + `/events`、`goals` 4 条、`tasks` 4 条 | `Work.vue`（51 行）**只读列表**，不能新建/编辑/删除，无目标页 |
| 个人画像条目 | `profile-items` 9 条（含 propose/confirm/reject/expire/history） | `Persona.vue`（45 行）只读 `/api/profile` 三字段，**画像条目能力整体未接** |
| 建议状态机 | accept/ignore/reject/reopen/delete、outcome 3 条、statistics、runs | `Recommendations.vue` 只有投票与备注，**无法采纳/忽略**，且逐条串行拉 feedback（N+1） |
| 连接器 | credential PUT/DELETE、pause/resume/revoke/schedule、source DELETE | `Connectors.vue` 只做新增/改名/同步/日志，**无法保存令牌**，私有仓库与鉴权 HTTP 源不可用 |
| 记忆 | PATCH、expire、history | `Memories.vue` 只有新增/删除/候选审批 |
| 行为分析 | `/api/analytics/comparison`、`/api/behavior/backfill` | 未接 |
| 提醒设置 | 12 个字段（含安静时段、复盘时间、周几） | `Settings.vue` 只渲染 5 个，且其中 3 个字段名不存在（E1） |

### E12 `index()` 强制 no-store，静态资源无缓存策略

`app/main.py:1089-1095` 对 HTML 关闭缓存是合理的（历史有"页面卡在正在检查服务"的教训），但 `StaticFiles` 挂载未设置 `Cache-Control`，带 hash 的 `assets/*` 每次仍走协商缓存。建议给 `/static/assets` 加 `max-age=31536000, immutable`。

---

## 4. 架构与工程化

### E13 `main.py` 仍是"胖入口"

- 3,461 行 / 162 KB；88 个 `@app.*` 路由；27 张 `CREATE TABLE`；35 个索引；5 个手写迁移函数（`_migrate_reminder_scheduler_runs`、`_migrate_memories`、`_migrate_data_sources`、`_migrate_documents`、`_migrate_recommendations`）。
- 代码里留有 `# ===== ... (inserted block begin/end) =====` 共 6 组标记（`:2047`、`:2080`、`:2116`、`:2438`、`:3070`），属于增量拼装痕迹，不是按领域组织。
- 7 处 `except Exception`，14 处 `except Exception as exc`；`db()`（`:172-182`）异常路径不做显式 `rollback()`，依赖 `close()` 隐式回滚。

建议：先按路由前缀切 5 个 `APIRouter`（reminders / connectors / recommendations / profile-items / work），`main.py` 只留 app、lifespan、中间件、`include_router`；schema 抽到 `app/schema.py` 并加 `PRAGMA user_version` 作为迁移版本号。

### E14 缺版本控制与质量门禁

- 当前目录**不是 Git 仓库**（`git status` → `fatal: not a git repository`），但 `AGENTS.md`/`docs` 多处引用 git 操作与"提交说明标注 skipped speckit"。
- 无 `.github/workflows`、无 `ruff`/`black`/`mypy` 配置、`pyproject.toml` 只有 pytest 段。
- 前端无测试框架：`frontend/package.json` 只有 dev/build/preview；`e2e_regression.mjs` 硬编码 `http://127.0.0.1:18765`，需手工起服务与执行。

建议：`git init` + 基线提交；加 `ruff` + `pytest` 的 pre-commit 与最小 CI；给 `package.json` 加 `"test": "vitest run"` 与 `"e2e": "node e2e_regression.mjs"`，并把 BASE 改为环境变量。

### E15 Spec Kit 流程只落地了基线

- `specs/` 下只有 `001-mvp-baseline`，`.specify/feature.json` 指向它。
- phase 2–5（长期记忆、数据中心、行为分析、建议、提醒、通知）在 `docs/plans/` 有成套设计，但没有对应 `specs/<编号>/`。
- 结论：`AGENTS.md` 里"任何功能性变更必须走 specify→plan→tasks→implement"的强制条款，在最近 4 个阶段实际未被遵守。

### E16 版本号三处不一致

`pyproject.toml:3` = 0.3.0、`app/main.py:101` = 0.3.0、`frontend/package.json:4` = 0.4.0。

### E17 仓库杂物

- 根目录存在 0 字节文件 `({text`（重定向语法错误的产物）。
- `temp/` 残留 8 个调试产物：`catchup_debug2.db`、`catchup_debug3.db`、`e2e_verify.db`（各 350–370 KB）、`debug_lease.py`、`debug_lease2.py`、`debug_out*.txt`、`reminder_verify.py/json`、`server_*.log`。

### E18 审批功能只有壳

`app/main.py:3428-3461` 三个路由只做 CRUD，全仓库没有任何消费 `approvals` 的执行器（`grep "FROM approvals"` 只命中这三处）。"操作审批"页签目前只是写入与展示，批准后不产生任何行为。

---

## 5. 性能与检索

### E19 `retriever.py` 是全表扫描 + Python 打分

- `search_memories`：`LIMIT 300`（`_MEMORY_LIMIT`）→ 记忆超过 300 条时**旧记忆永远检索不到**（按 `updated_at DESC` 截断）。
- `search_documents`：`LIMIT 100`，同样有召回截断。
- `search_chunks`：**无 LIMIT**，把全部 chunk 连同 `documents` join 出来，逐行 `tokenize(text)`；`tokenize` 每次调用都重新分词，`query` 的 token 也在循环内重复计算（`:104-109`）。
- 后果：文档库到几千 chunk 时，每次 `/api/chat` 的 `retrieve_context()` 会做 O(N) 分词，单次请求延迟线性上升；同时语义质量受限于二元词重叠。

建议（不改接口）：先用 SQLite FTS5 建 `document_chunks_fts` 虚表 + `bm25()` 排序，把候选集压到 200 以内再做现有 boost；`tokenize` 结果按 query/row 缓存。这是"低改动、可回退"的路径。

### E20 聊天链路

- `app/main.py:3382-3402`：非流式（`POST /api/chat` 一次性返回），历史只取 8 条（`:3393`），`evidence_block` 每条截断 1000 字符但**总预算未限制**（`:944-1000`），长文档集可能直接超上下文。
- `_run_memory_extraction`（`:1076-1086`）在响应返回前 `await`，把一次额外 LLM 调用串到用户等待时间里；异常被吞掉只记 warning（合理），但延迟不可接受。
- `httpx.AsyncClient(timeout=60)` 每次调用新建（`:1024` 附近），无连接复用；重试 5 次但没有明确退避。
- `list_approvals`（`:3438-3444`）无分页，`list_documents`/`list_conversations` 同类问题。

建议：改成 SSE 流式；记忆抽取改为 `BackgroundTasks` 或独立队列；限制 evidence 总字符预算（如 6000）；复用全局 `AsyncClient`。

---

## 6. 数据与文档一致性

### E21 启动即把真实库切到 WAL，与文档/备份指引冲突

- `app/main.py:341-342` 在 `init_db()` 内执行 `PRAGMA journal_mode = WAL` + `synchronous = NORMAL`。
- 实测当前库头字节 18/19 = `1/1`（rollback journal），`data/` 下无 `-wal`/`-shm`。
- `README.md:197` 写"当前真实开发库为 SQLite delete journal 模式；未来若切换 WAL，备份需包含 `-wal` 和 `-shm`"。
- 后果：**下一次应用启动就会静默把真实库切成 WAL**，此后按现文档做的备份（只拷 `.db`）会丢最近事务；`docs/database.md` 同类描述也需同步。

建议二选一并写进 `docs/decisions.md`：
1. 保留 WAL（推荐，调度器/通知/HTTP 三写者共存），更新 README/`docs/database.md` 备份指引为"停服后拷贝 `db + -wal + -shm`，或用 `VACUUM INTO`"；
2. 去掉这两行，明确长期使用 rollback journal，并说明单写者假设。

另：`init_db()` 里的 `PRAGMA journal_mode=WAL` 只在建立连接时调用，其他请求连接（`db()`）不会重复设置——这本身没问题（WAL 是持久属性），但"谁在什么时候切换了日志模式"没有任何日志记录，建议加一行 `logger.info` 记录实际 journal_mode。

### E22 文档数值与实测不符

- `README.md:179`：`445 passed, 1 skipped, 1 warning`（C-C-2c 历史结果）。
- 实测：`20 failed, 467 passed, 1 warning`，总用例数已从 446 涨到 487。
- `docs/testing.md` 第 0 节仍以 C-C-2d 为"当前结论"，而 `docs/progress.md` 已到 2026-10-14 的 P0-4 研究，二者时间线不一致。

---

## 7. 建议执行顺序（每步都可独立验证）

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 修 E2（3 处 `exc.message` → `str(exc)`） | 两个探测请求返回 400 而非 500 |
| 2 | 修 E1（删白名单）+ 修 Settings.vue 字段 | 设置页保存后 GET 回读一致；补齐安静时段 |
| 3 | 修 E3（释放用 `current_dt`） | `pytest tests/test_reminder_scheduler*.py tests/test_notification_delivery.py -q` 全绿 |
| 4 | 定位 E5 并处理（或标记 skip + 单独 issue） | 全量 `pytest -q` 无 failed |
| 5 | 修 E4（snooze 写 `snoozed_until`）+ API/前端补时长 | 新增专项测试：snooze 后 tick 不立即转 due |
| 6 | 把测试里的固定日期改为相对 `utc_now()` 的时间夹具 | 模拟"未来 30 天"仍全绿 |
| 7 | 同步文档：README/AGENTS/architecture/module-responsibilities 改 Vue + Vite；更新测试数字；决定 WAL 并改备份指引 | 文档 grep 无"无构建" |
| 8 | 清死代码：删 7 个原生 JS、旧 hash 产物；`vite.config.ts` 设 `emptyOutDir: true`（或改输出到独立目录） | `npm run build` 后 `assets/` 只剩当前产物 |
| 9 | 前端补关键缺口（建议优先级）：建议采纳/忽略 → 项目任务 CRUD → 连接器凭据 → 画像条目 | 每个能力有对应 UI 动作 |
| 10 | 拆分 `main.py` 为 router + schema 模块；加 `user_version` | 路由数/行为不变，`pytest` 全绿 |
| 11 | 引入 `git init` + ruff + 最小 CI + FTS5 检索 | CI 绿；`/api/chat` 在 1 万 chunk 下延迟可测 |

## 8. 未覆盖 / 未执行

- 未执行前端构建与 E2E，未验证打包产物与 `src` 完全一致（仅抽查文案命中）。
- 未启动 Uvicorn，未验证 lifespan 下调度器/通知服务的真实启停与 health 输出。
- 未验证连接器对真实 GitHub / HTTP 源的同步（按项目规则应用 mock）。
- 未对真实开发库做任何写入、迁移或 `init_db()` 调用；WAL 结论来自头字节静态读取。
- E5 根因未定位，仅给出复现命令与排查方向；不作为已确认缺陷计入修复清单。

---

## 9. 修复进展（2026-10-21 21:45 CST 更新）

本报告是审查快照。后续修复状态以 `docs/current-task.md` 与 `docs/testing.md` 第 0 节为准，下表只记录已经关闭的条目。

| 条目 | 状态 | 证据 |
|---|---|---|
| E1 设置接口字段错位 | 已修 | `app/main.py` 改为以 `reminders.SETTINGS_FIELDS` 为唯一真源；真实字段 200 且回读一致，旧字段 400 |
| E2 `exc.message` → 500 | 已修 | 三处改 `str(exc)`；非法 action / status 实测 400 |
| E3 租约释放时钟混用 | 已修 | `run_tick` 释放改用 `current_dt`；`_lease_is_free` 与 acquire UPDATE 视 `released_at` 为可用 |
| E4 snooze 无唤醒时间 | 已修 | 写入 `snoozed_until`（默认 10 分钟/上限 7 天）；新增 2 个回归测试 |
| E5 readonly 并发失败 | 已定性 | 最小复现：全新 WAL 库首轮 3 线程写 2/8 失败，DELETE journal 0/8，同一 WAL 库连续 12 轮 0/12。并发用例改用 rollback journal |
| E8 WAL 与文档冲突 | 已修 | 保留 WAL；README / `docs/database.md` 备份指引改为 db+-wal+-shm 或 `VACUUM INTO`；`init_db()` 打印实际模式 |
| E9 前端技术栈文档失真 | 已修 | README / AGENTS / architecture / module-responsibilities 改 Vue 3 + Vite |
| E10 旧 hash 构建产物 / 原生 JS 死代码 | 已关闭 | 2026-10-22：删除 7 个原生 JS（2,311 行）与全部旧 hash 产物，`vite.config.ts` 改 `emptyOutDir: true`；`app/static` 只剩 `index.html` + 1 JS + 1 CSS。原先读这些文件的 4 个断言改为读构建产物与 `frontend/src`（无 `v-html`/`innerHTML`/`onclick=`、产物与引用一致） |
| E22 测试数值失真 | 已修 | README / `docs/testing.md` 更新为 `488 passed, 1 skipped`（本批结束再跑为 489 passed） |

未关闭：E11 前端能力断层、E13 `main.py` 拆分、E14 版本控制/CI、E15 Spec Kit、E16 版本号、E17 杂物、E18 审批空壳、E19 检索全表扫描、E20 聊天链路。

审查时的 "待定位" 结论已在 E5 行更新为已定性；原第 2 节 E5 段落保留作为排查过程记录。
