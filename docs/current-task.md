# 当前任务

更新时间：2026-10-22 13:25 CST

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

## 唯一下一步

**FTS5 检索与聊天流式输出**（审查报告最后的两个功能项）。FTS5：`document_chunks` 表建虚拟表 + 触发器同步，`retriever.search_chunks` 换 MATCH 查询，注意中文分词（FTS5 默认 unicode61 对中文按字切分，可用 trigram 或保持现状）。流式输出：`/api/chat` 改 SSE（`StreamingResponse`），前端 `Chat.vue` 用 fetch + ReadStream 渲染；`call_llm` 已有 httpx，流式读 `response.aiter_lines()`。

### 再往后

1. phase 3–5 spec 补齐或豁免。
2. 可选：给 `app/api/` 各 router 补 `tags` 便于 OpenAPI 文档分组。 项目至今不是 git 仓库——七批 feature 的回滚全靠 `temp/bak-*` 手工备份，这在真实开发里不可持续。

| 步骤 | 内容 |
| --- | --- |
| 1 | `git init` + 首次提交（`temp/`、`data/`、`.venv/`、`__pycache__` 进 `.gitignore`；`data/secret.key` 绝不入库） |
| 2 | `ruff check app tests` 清理现有告警（预期主要是 import 顺序与未用变量），配置进 `pyproject.toml` |
| 3 | GitHub Actions：`compileall + pytest + npm ci && npm run build`，Windows 与 Linux 各跑一遍（0.6 节的 READONLY 竞争是 Windows 特有，CI 双平台才有意义） |

注意：初始化 git 前先向用户确认——这是仓库级变更，且需要用户决定远端托管位置。

### 再往后

1. `app/main.py` 拆分（3,479 行 / 88 路由）。
2. FTS5 检索、聊天流式输出。
3. phase 3–5 spec 补齐或豁免。
