# 当前任务

更新时间：2026-10-22 14:00 CST

## 任务目标

按 `docs/audits/2026-10-21-full-project-review.md` 的建议顺序推进缺陷修复与优化，遇到需要产品决策的点先向用户确认。

## 当前真实状态：审查报告的可交付项已全部收口

- 2026-10-21 缺陷修复批次（E1–E5、E8、E9、E22）：`docs/testing.md` 第 0 节。
- 0.1 前端死代码清理；0.2 feature 002 建议闭环；0.3 feature 003 工作项 CRUD；0.4 feature 004 建议结果登记与统计洞察；0.5 feature 005 画像条目管理（含一处后端日期清空修复）。
- 0.6 **flaky 根因修复**：`test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 实为真实缺陷（全新 WAL 库首个写连接瞬时 `SQLITE_READONLY`），修复为租约获取阶段的有界换连接重试；新增 `tests/test_reminder_scheduler_retry.py`。
- 0.7 feature 006 提醒延期交互 + 行为周期对比。
- 0.8 feature 007 连接器凭据管理：`Connectors.vue` 凭据徽标 + 设置/更换/清除（write-only 纪律）+ GitHub PAT 创建字段；决策见 `docs/decisions.md`。

验证快照：

```text
compileall        → 0
pytest -q         → 569 passed, 1 warning
npm run build     → ✓ 3.81s，app/static 仅 index.html + 1 JS + 1 CSS
Playwright 冒烟    → 建议 15；工作项 36；洞察 29；画像 23；snooze/对比 15；凭据 12（各全过）
lifecycle 文件复跑 → 8 次连跑全绿（0.6 节修复后）
真实库             → sha256=008ecb5c…ddfb、size=389120、mtime=2026-10-14 23:11:17，始终未被写入
```

## 已完成（本轮新增）

- **CI 首跑全绿**（run 39720882736）：ubuntu 38s / windows 2m41s / frontend 13s，Linux 平台差异为零。
- **main.py 拆分完成**：821 行 + `app/api/` 九个域 router + `app/schema_migrations.py`。打点兼容设计（`_root` 晚绑定 + re-export）使 55 处 `app.main.DB_PATH` patch 与全部既有测试零改动通过。详见 `docs/testing.md` 0.10 节。

## 已完成（本轮新增）

**feature 008：FTS5 文档检索 + 聊天流式输出**（`specs/008-fts-and-streaming/`，579 passed）。

- FTS：`_migrate_chunks_fts`（trigram external-content 表 + 触发器 + 幂等回填）；`retriever._fts_boost` bm25 加成、异常静默回退 lexical。
- 流式：`POST /api/chat/stream` SSE（meta/delta/done/error），`Chat.vue` fetch + ReadableStream 增量渲染。
- **E1 教训**：后台播种必须显式设 `DATABASE_PATH`，否则 `import app.main` 会打到真实库（本次仅幂等 DDL，数据完好，见 0.11 节）。

## 已完成（本轮新增）

- OpenAPI tags：9 个 router 分 9 组（84 操作），`/docs` 可按域浏览。
- phase 3–5 spec 收口：009（数据接入，已实现对照）/ 010（分析建议，已实现对照）/ 011（受控 Agent，按安全边界豁免）；`docs/PLAN.md` 阶段状态同步。

## 唯一待办

**真实 LLM Key 联调流式链路**——需要用户提供 `LLM_API_KEY` + `LLM_BASE_URL`（OpenAI 兼容端点）。SSE 解析已有单测，未打过真实端点；联调时重点看：delta 边界、`[DONE]` 处理、网络中断时 error 事件。
