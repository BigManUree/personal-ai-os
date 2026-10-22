# 当前任务

更新时间：2026-10-22 15:05 CST

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

**知识库删除 500 修复 + 对话历史删除**（0.19 节）：FTS 失同步自愈（rebuild + 重试）、真实库已预修复、`DELETE /api/conversations/{id}` + Chat.vue 悬停删除/内联确认。582 passed。

## 唯一下一步

无待办。
