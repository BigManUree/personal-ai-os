# 当前任务

更新时间：2026-10-22 12:10 CST

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

- Git 初始化 + 首次提交 `b3388af` + 推送 `origin/main`（`https://github.com/BigManUree/personal-ai-os.git`）。
- ruff 全绿（配置见 0.9 节），顺带修掉一个真缺陷：`NotificationConfigError` 被引用但从未定义。
- `.github/workflows/ci.yml` 双平台 CI 就位。
- 播种脚本收编进 `scripts/e2e/` 并入库，文档引用同步。

## 唯一下一步

**观察 CI 首跑并修平台差异。** 推送已触发 Actions（若仓库已启用 CI）：确认 ubuntu 与 windows 两个 backend job、frontend job 的结果。预期风险点是 Linux 上个别测试的路径/时区假设。如有失败，按失败清单逐个修——它们就是"换平台才会暴露"的真实缺陷。 项目至今不是 git 仓库——七批 feature 的回滚全靠 `temp/bak-*` 手工备份，这在真实开发里不可持续。

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
