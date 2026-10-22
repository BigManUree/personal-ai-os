# 2026-10-21 修复批次：改动清单与回退路径

批次目标：关闭审查报告 `docs/audits/2026-10-21-full-project-review.md` 的 E1–E5、E8、E9、E10（部分）、E22。

## 1. 备份位置

`temp/` 下保留了本批开工前的原始副本（回退时直接覆盖即可）：

| 备份 | 对应文件 |
|---|---|
| `temp/backup-main.py.bak` | `app/main.py` |
| `temp/backup-reminder_scheduler.py.bak` | `app/reminder_scheduler.py` |
| `temp/bak-README.md` | `README.md` |
| `temp/bak-AGENTS.md` | `AGENTS.md` |
| `temp/bak-architecture.md` | `docs/architecture.md` |
| `temp/bak-module-responsibilities.md` | `docs/module-responsibilities.md` |
| `temp/bak-api.py` | `tests/test_recommendation_feedback_api.py` |
| `temp/bak-store.py` | `tests/test_recommendation_feedback_store.py` |
| `temp/bak-stats.py` | `tests/test_recommendation_stats.py` |
| `temp/backup-static-index.html.bak` | `app/static/index.html`（构建前） |

回退示例：

```powershell
Copy-Item temp/backup-main.py.bak app/main.py -Force
Copy-Item temp/backup-reminder_scheduler.py.bak app/reminder_scheduler.py -Force
```

## 2. 逐文件改动（原始 → 目标）

### app/main.py

1. `PUT /api/reminder-settings`（约 1441 行）
   - 原始：本地硬编码白名单 `daily_review_enabled / weekly_review_enabled / recommendation_followup_enabled / task_due_soon_enabled / task_overdue_enabled / task_stalled_enabled / review_time_local / timezone`，`update_settings(conn, allowed)`。
   - 目标：`editable = set(reminders.SETTINGS_FIELDS)`；未知字段（排除 `created_at`/`updated_at`）→ 400；`allowed` 由 `editable` 过滤。
2. 三处 `detail=exc.message` → `detail=str(exc)`（`list_reminders_api`、`transition_reminder_api`、`update_reminder_settings_api`）。
3. `transition_reminder_api` 增加 `snooze_minutes = body.get("snooze_minutes", 10)` 并传入领域函数。
4. `init_db()`：新增一行 `logger.info("sqlite journal_mode=%s database=%s", ...)`。

### app/reminders.py

1. 新增常量 `MAX_SNOOZE_MINUTES = 7 * 24 * 60`。
2. `transition_reminder(...)` 增加 `snooze_minutes: int = 10`，并对 `snooze` 校验 1..10080（拒绝 bool / 非 int）。
3. `ts = iso(now or utc_now())` → `reference = now or utc_now(); ts = iso(reference)`。
4. `snooze` 分支新增 `snoozed_until = reference + timedelta(minutes=snooze_minutes)`。

### app/reminder_scheduler.py

1. `test` 无关的运行时改动：`release_lease(..., now=current_dt, ...)`（原 `now=lease_svc.utc_now()`）。
2. `transition_reminder_guarded(...)` 增加 `snooze_minutes` 透传。

### app/reminder_scheduler_lock.py

1. `_lease_is_free`：新增 `if row["released_at"]: return True`。
2. acquire 的条件 `UPDATE ... WHERE`：新增 `OR released_at IS NOT NULL`。

### frontend/src/pages/Settings.vue

- 整文件重写：字段改为 `task_due_enabled / task_stalled_enabled / recommendation_followup_enabled / daily_review_enabled / daily_review_at / weekly_review_enabled / weekly_review_weekday / weekly_review_at / quiet_hours_enabled / quiet_start / quiet_end / timezone`；保存时只提交这些字段。

### app/static/（构建产物）

- `index.html` 由 `npm run build` 重新生成，引用 `/static/assets/index-CP7wDYvj.js` 与 `/static/assets/index-DzMDiI6T.css`。
- 删除旧产物：`index-BG2H5jI2.js`、`index-BvWh3s6A.js`、`index-uneSmSWi.js`、`index-C9lmxsWo.css`、`index-CNkHeUzY.css`。
- 回退方式：恢复 `temp/backup-static-index.html.bak`，并 `cd frontend && npm run build` 重新生成（旧 hash 文件不可恢复，但可从源码重建；旧 `index-BvWh3s6A.js` 对应提交前的 `src/` 状态，需先回退 `Settings.vue`）。

### tests/

| 文件 | 改动 |
|---|---|
| `conftest.py` | 新增 `rollback_journal` fixture（接受连接或路径，执行 `PRAGMA journal_mode=DELETE`） |
| `test_reminders_store.py` | 并发用例改用 fixture |
| `test_reminder_scheduler_lock.py` | 同上 |
| `test_reminder_scheduler_reliability.py` | 同上（`init_db()` 后切 rollback） |
| `test_recommendation_feedback_store.py` | 同上 |
| `test_reminder_scheduler_fencing.py` | 新增 `_hold_lease`；两个竞争用例改为"holder 持有 → 竞争者全 busy → 释放 → 单次执行"；`_run_tick_child_pair` → `_run_tick_children(db_path, owners)` |
| `test_reminder_scheduler.py` | 追加 `test_snooze_sets_wake_time_and_tick_waits_for_it`、`test_snooze_rejects_out_of_range_minutes` |
| `test_reminder_scheduler_lifecycle.py` | lifespan 用例全程持 warm 连接；跨进程恢复用例子进程改轮询 scheduled run |
| `test_recommendation_feedback_api.py` / `_store.py` / `_stats.py` | 硬编码 `2026-10-20` 改 `FUTURE_RESUME = now + 30d` |

### 文档

`README.md`（技术栈/目录/构建步骤/测试数字/备份指引）、`AGENTS.md`（前端边界）、`docs/architecture.md`、`docs/module-responsibilities.md`、`docs/database.md`（WAL 与备份）、`docs/decisions.md`（3 条新决策）、`docs/progress.md`、`docs/current-task.md`、`docs/testing.md`（第 0 节）、`docs/audits/2026-10-21-full-project-review.md`（第 9 节进展）。

## 3. 验证回执

```text
compileall                                  → 退出码 0
pytest -q（连续三次）                        → 488 passed, 1 skipped / 488 passed, 1 skipped / 489 passed
GET /                                       → 200，引用 index-CP7wDYvj.js + index-DzMDiI6T.css
GET /static/assets/index-CP7wDYvj.js        → 200 application/javascript 117469 B
GET /static/assets/index-DzMDiI6T.css       → 200 text/css 20949 B
data/personal_ai_os.db 头字节 18/19          → 1/1（未切 WAL），mtime 仍为 2026-10-14 23:11
```

## 4. 未做（需要另行确认）

- 前端能力补齐、`main.py` 拆分、Git/CI/lint、FTS5 检索、撤回根目录 `({text` 与 `temp/` 调试文件。
- 本批按"缺陷修复"口径未走 `specs/` 工作流，未补 spec。

---

## 5. 追加批次（2026-10-22）：前端死代码清理

### 5.1 删除的文件

| 路径 | 行数 | 删除前 SHA-256 前 16 位 |
|---|---|---|
| `app/static/analytics.js` | 165 | `db9649fcde673fdf` |
| `app/static/connectors.js` | 232 | `720a165fbeefa98e` |
| `app/static/doc-upload.js` | 48 | `47d4099faade6e90` |
| `app/static/memory-candidates.js` | 92 | `9e557e99f3b33e7f` |
| `app/static/persona.js` | 204 | `ddf1a7098d6ba1d6` |
| `app/static/recommendations.js` | 703 | `9d22f08eaa0a5506` |
| `app/static/work.js` | 203 | `30553f3635068a10` |

完整副本在 `temp/legacy-static-js-backup/`（7 个文件，合计 2,311 行）。

### 5.2 配置改动

`frontend/vite.config.ts`：

```diff
   build: {
     outDir: '../app/static',
-    emptyOutDir: false,
+    emptyOutDir: true,
     assetsDir: 'assets',
   },
```

注意：`emptyOutDir: true` 会清空 `app/static` 整目录，因此该目录只能放 Vite 产物。

### 5.3 测试改动

`tests/conftest.py` 新增 session 级 fixture `frontend_bundle`（读取 `index.html` 引用的 JS）与 `frontend_sources`（拼接 `frontend/src/**/*.vue|*.ts`）；4 个原读取已删除脚本的测试改为断言构建产物与源码，明细见 `docs/testing.md` 第 0.1 节。

### 5.4 回退方式

1. 恢复脚本：把 `temp/legacy-static-js-backup/` 下的 7 个 `.js` 复制回 `app/static/`。
2. 恢复配置：把 `frontend/vite.config.ts` 第 9 行的 `emptyOutDir: true` 改回 `false`。
3. 恢复测试：`tests/test_analytics_api.py`、`tests/test_recommendations_api.py`、`tests/test_recommendation_feedback_api.py`、`tests/test_recommendation_stats.py` 的 4 个断言按 `docs/testing.md` 第 0.1 节的对照表反向改回（读旧脚本）。
4. 重新构建：`cd frontend && npm run build`；`emptyOutDir=false` 时不会自动清理，需要手动删除未引用的 hash 文件。

回退后 7 个脚本会重新出现在 `app/static`，但 `index.html` 仍只引用 Vue 产物，它们不会被加载，只是恢复文件存在性。

### 5.5 验证回执

```text
compileall                      → 退出码 0
pytest -q                       → 488 passed, 1 skipped, 1 warning (109.7s)
npm run build                   → ✓ built in 4.16s
app/static 文件清单              → index.html + assets/index-CP7wDYvj.js + assets/index-DzMDiI6T.css
plant stale-test.js + rebuild   → stale-test.js 被清空，目录仍是上述 3 个文件
GET /                           → 200
GET /static/analytics.js        → 404
```
