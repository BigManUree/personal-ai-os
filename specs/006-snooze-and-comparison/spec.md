# 006 · 提醒延期交互与行为周期对比

更新时间：2026-10-22

## 背景

两处「后端有、前端没有入口」的缺口：

1. `POST /api/reminders/{id}/transition` 已支持 `{action:"snooze", snooze_minutes}`（`1..10080`，且只有 `due` 状态可延期），`frontend/src/pages/Reminders.vue` 的 `act()` 只发 `{action}`，界面无延期按钮，`snoozed_until` 也从不显示。
2. `GET /api/analytics/comparison?window_days&timezone` 已实现等长（7/7 或 30/30）相邻窗口对比，`frontend/src/pages/Analytics.vue` 只调 `/analytics/overview`，对比结果无入口。

两者都属于「已有接口缺 UI」，后端语义不改。

## 目标

- 提醒卡片按**当前状态**给出合法动作集合，延期可选分钟数，并且看得见下次唤醒时间。
- 行为洞察页加入周期对比：本期 / 上期 / 变化量 / 变化幅度 / 是否显著，`percent_change == null` 时显示「—」。

## 功能需求

### 提醒（Reminders.vue）

- **FR-101** 动作集合由状态决定，必须是服务端 `TRANSITIONS` 的**子集**（否则按钮点下去必 409）：

  | 状态 | 界面动作 | 服务端额外允许但界面不提供 |
  |---|---|---|
  | `due` | `acknowledge`、`snooze`、`complete`、`dismiss`、`cancel` | `expire`（调度器写） |
  | `snoozed` | `complete`、`dismiss`、`cancel` | `expire`、`snooze`（重复延期无意义） |
  | `acknowledged` | `complete`、`dismiss`、`cancel` | `acknowledge`（幂等空操作） |
  | `scheduled` | `cancel` | `expire` |
  | 终态（`completed`/`dismissed`/`cancelled`/`expired`） | 无 | 无 |
  | — | — | `mark_due`（调度器内部动作，永不出现） |

  排除项在 `tests/test_snooze_and_comparison_ui.py` 的 `HIDDEN_ACTIONS` 中显式列出：新增/删除一个被隐藏的动作必须同时改测试，不会静默漂移。

- **FR-102** 延期使用内联面板：预设 15 分钟 / 1 小时 / 3 小时 / 1 天 + 自定义分钟（1..10080 整数）。提交体为 `{"action":"snooze","snooze_minutes":N}`，不使用默认值 10 的隐式行为。
- **FR-103** `snoozed` 卡片显示 `snoozed_until`（本地时间 `YYYY-MM-DD HH:mm`）；缺失时显示「—」而不是留空。
- **FR-104** 取消必须显式选原因，原因取自服务端 `CANCEL_REASONS` 的 UI 子集（默认 `user_cancelled`）。
- **FR-105** 失败时展示后端 `detail` 原文（如 409 的状态机冲突），并重新拉取列表，避免界面与服务端状态漂移。
- **FR-106** 状态显示中文标签，未知状态回退原值。

### 对比（Analytics.vue）

- **FR-111** 窗口切换 7 / 30 天，请求 `/api/analytics/comparison?window_days=N&timezone=Asia/Shanghai`。
- **FR-112** 表格列出 `metrics` 的每项：指标名（中文映射，未知回退键名）、上期、本期、变化量（`delta`）、幅度（`percent_change`）、显著性标记（`significant`）。
- **FR-113** `percent_change === null` 显示「—」；不得用 `0%` 代替（零基数时百分比无定义）。
- **FR-114** 展示两段时间范围（`current.start..end`、`previous.start..end`）与 `definition_version`，让人能核对口径。
- **FR-115** 对比接口返回 400（非法窗口）时展示 `detail`，不清空已有数据。

## 非目标

- 不改提醒状态机、不改 `MAX_SNOOZE_MINUTES`。
- 不改对比口径与显著性阈值（`MIN_STRONG_SAMPLE`、20% 门限）。
- 不做图表库引入，不加批量操作。

## 验收

- 浏览器冒烟覆盖：due 卡片延期 60 分钟后状态变 `snoozed` 且 `snoozed_until` 前进 60 分钟；非法动作（对 `completed` 再 complete）返回 409 且界面显示 detail；对比表在零基数指标上显示「—」。
- 源码契约测试锁住：动作集合、`snooze_minutes` 透传、`percent_change` 空值处理、窗口取值 7/30。
