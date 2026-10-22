# Plan · 006

## 组件

| 组件 | 职责 |
|---|---|
| `frontend/src/pages/Reminders.vue` | 状态 → 动作集合、延期内联面板、取消原因、`snoozed_until` 显示 |
| `frontend/src/pages/Analytics.vue` | 保留 overview，追加对比区块（窗口切换 + 表格） |
| `tests/test_snooze_and_comparison_ui.py` | 源码/契约断言：动作表、分钟数透传、空值渲染、窗口值 |

后端零改动（本批真正的后端改动在调度器重试，见 `docs/testing.md` 0.6 节与 `docs/decisions.md`）。

## 动作表来源

服务端 `app/reminders.py:51-59` 的 `TRANSITIONS` 是唯一权威。前端不重新推导，直接照抄为常量并加注释指向源文件行号；`test_snooze_and_comparison_ui.py` 用 Python 解析 `TRANSITIONS` 与前端常量比对，任一侧漂移立即变红。

## 延期交互

- 点击「延期」展开内联面板（不是 modal，避免多层遮罩）。
- 预设按钮直接提交；自定义输入走 `form` 提交（回车/按钮均可）。
- 提交前本地校验：整数、`1..10080`，非法时 toast 且不发请求。
- 成功后关闭面板并刷新列表。

## 对比表渲染

- `percent_change` 为 `null` → `—`；`0` → `0.0%`（真实无变化）。
- `direction` 为 `up`/`down` 时不使用红绿（本项目非行情场景），只用箭头 `↑ / ↓ / →` 与文字。
- `significant` 用「显著」标签，否则不渲染标签。

## 验证

1. `pytest tests/test_snooze_and_comparison_ui.py -q`
2. `pytest -q` 全量
3. `npm run build`
4. Playwright 冒烟：播种临时库（含 due/snoozed/终态提醒各一条）→ 延期、非法动作、对比表零基数。
5. 真实库指纹前后比对。

## 回滚

- `temp/bak-reminders-vue-006.vue`、`temp/bak-analytics-vue-006.vue` 为改动前备份。
- 删除 `tests/test_snooze_and_comparison_ui.py` 与 `frontend/e2e_snooze.mjs`，还原两个 .vue，`npm run build` 即回到改动前。
