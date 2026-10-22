# Feature Specification: 建议结果登记与统计洞察

**Feature Branch**: `004-recommendation-insights`

**Created**: 2026-10-22

**Status**: Implemented

**Input**: 用户指令「按你的建议进行下一步任务并把它下一步做完」。审查报告 E11 的第三项：后端已有 outcome / 统计 / runs 三组接口，前端零调用点。

## 背景

以下接口在 `app/main.py` 已存在且有测试覆盖，但 `frontend/src` 内没有任何调用（源码检索为空）：

| 路由 | 位置 | 语义 |
| --- | --- | --- |
| `GET /api/recommendations/{id}/outcome` | `app/main.py:3326` | 返回 `{outcome: {...}|null}` |
| `PUT /api/recommendations/{id}/outcome` | `app/main.py:3336` | 部分更新（`exclude_unset=True`），返回 `{status, outcome}` |
| `DELETE /api/recommendations/{id}/outcome` | `app/main.py:3356` | 删除，返回 `{deleted: true}` |
| `GET /api/recommendation-statistics` | `app/main.py:3121` | 只读统计快照 |
| `GET /api/recommendation-runs` | `app/main.py:3368` | 生成历史，`limit` 1..100 |

结果是：用户采纳建议后无法登记「到底有没有用」，也看不到建议质量与生成健康度。

## 关键契约（实现必须遵守，与上批相反）

**`PUT /api/recommendations/{id}/outcome` 是部分更新，不是全量替换。** 与工作项 PATCH 相反：服务层用 `_existing_outcome_data()` 合并未提供的字段（`app/recommendation_feedback.py:526-`），并用 `data.model_fields_set` 判断"用户是否显式提供了该字段"。

由此派生三条约束：

1. **首次保存必须带 `execution_status`**：`OutcomeDraft.execution_status` 无默认值，缺失即 422/400。
2. **`metric_observations` 要么不发，要么发完整列表**：服务端对提供的列表整体覆盖，不会按索引合并。前端保存时总是发送表单里的完整列表（含 0 条），以保证"删除某行"能生效。
3. **写操作要求建议处于 `accepted` 且未过期**，否则 409（`_require_accepted_active_for_outcome_mutation`，`app/main.py:3273`）。撤销采纳后历史结果保留但不可修改。

### 枚举（以 `app/recommendation_feedback.py:29-59` 为唯一真源）

- `execution_status`：`planned` / `completed` / `partially_completed` / `not_completed` / `no_longer_relevant`
- `result_rating`：`much_better` / `better` / `as_expected` / `worse` / `much_worse` / `unclear`
- `metric`（9 个）：`documents_created`、`project_activities`、`goals_created`、`tasks_created`、`tasks_completed`、`tasks_reopened`、`tasks_cancelled`、`net_completion_ratio`、`connector_sync_success_rate`
- `unit`：`count` / `ratio` / `minutes`
- 固定值（前端不暴露开关）：`source=user_reported`、`attribution=not_inferred`、`outcome.source=user`
- `metric_observations` 最多 10 条；`actual_effect_note` 最长 1000 字

## User Scenarios & Testing

### User Story 1 - 登记采纳后的实际效果 (Priority: P1)

用户在「智能建议」页看到一条已采纳的建议，展开「登记结果」，选择执行状态与效果评级、写备注，保存后重新打开仍是这些值。

**Independent Test**: 临时库播种一条 `accepted` 建议，页面出现「登记结果」；选 `completed` + `better` + 备注，保存，`GET /api/recommendations/{id}/outcome` 返回同样三个值；刷新页面后表单回填。

**Acceptance**:

1. 只有 `status === 'accepted'` 的卡片显示登记入口；`pending`/`ignored`/`rejected`/`expired` 不显示。
2. 表单字段：执行状态（5 选，必填）、效果评级（6 选，默认 `unclear`）、备注（≤1000）、指标观察（可增删，≤10 条）。
3. 指标行字段：指标（9 选）、单位（3 选）、期望值、实际值（均可空）、`captured_at` 自动取当前时间、`source`/`attribution` 固定值不下发可见控件。
4. 首次保存必须选择执行状态；未选时本地拦截并提示。
5. 保存成功展示服务端返回的 `status`（`created`/`updated`）并回填最新值。
6. 已有结果时提供「删除结果」，二次确认后调 DELETE，`GET` 变回 `null`。
7. 服务端 409（非 accepted / 已过期）时展示服务端 detail，并提示"撤销采纳后结果不可修改"。
8. 建议已过期（`expires_at` 早于当前时间）时，保存按钮禁用并说明原因。

### User Story 2 - 查看建议质量统计 (Priority: P1)

用户展开「统计与历史」，选择时间窗口，看到推荐状态分布、反馈率、采纳率、结果登记率、评分分布与指标对比。

**Independent Test**: 切换 7/30/90 天，请求带 `start_at`/`end_at`，页面数字随之变化；空库时全部显示 `0` 与「—」，不报错。

**Acceptance**:

1. 窗口选项 7 / 30 / 90 天（服务端默认 `DEFAULT_WINDOW_DAYS = 30`），切换后重新请求。
2. 展示：推荐总数、状态分布（pending/accepted/ignored/rejected/expired）、证据覆盖率、反馈数/反馈率/有用率、当前采纳率、结果登记率、完成率、评分计数与比率、指标对比表（metric/unit/样本数/期望均值/实际均值/差值）、质量 `sample_status`。
3. 比率为 `null`（分母为 0）时显示「—」，不显示 `0%`，避免"没有数据"被误读成"效果为零"。**非 null 时服务端返回的是对象** `{numerator, denominator, value, sample_status}`（`app/recommendation_stats.py:118`），不是标量——渲染必须取 `.value`，并把 `numerator/denominator` 一并展示。
4. 展示 `limitations` 原文（服务端给出的四条口径说明），不自行改写。
5. `sample_status === 'insufficient_data'` 时给出显式提示（样本 < 10）。
6. 请求失败展示服务端 detail；`400 invalid_time_range` 有明确文案。

### User Story 3 - 查看生成历史 (Priority: P2)

用户在同一面板看到最近 10 次生成记录：时间、状态、是否用模型、窗口、候选/新增/重复/抑制/过期、失败原因。

**Acceptance**:

1. 请求 `GET /api/recommendation-runs?limit=10`。
2. 表格列：`started_at`（截断到分钟）、`status`、`window_days`、`candidates`、`created`、`duplicates`、`suppressed_rejected`、`expired_marked`、`use_model`、`error`。
3. `failed` 行的 `error` 以等宽字体展示，超长截断到 200 字符并保留 title 提示。
4. 无记录时显示「暂无生成记录」。

### Non-goals

- 不改统计口径、`DEFINITION_VERSION` 或任何后端计算逻辑。
- 不新增通知渠道、不导出 CSV、不做分页翻页（runs 固定最近 10 条）。
- 不做建议详情页跳转、不做跨建议批量登记。
- 不实现"因果关系推断"——`causality_status` 始终展示为 `not_inferred`。

## Functional Requirements

- **FR-001** 新建 `frontend/src/components/OutcomeForm.vue`，自行加载 `GET .../outcome`，不污染父级 `load()`。
- **FR-002** 新建 `frontend/src/components/RecommendationInsights.vue`，内部加载 statistics + runs，两个请求并行、各自降级。
- **FR-003** 保存时提交完整 `{execution_status, result_rating, actual_effect_note, metric_observations}`；不提交 `source`/`causality_status`（服务端固定）。
- **FR-004** 指标行的 `captured_at` 取浏览器当前时间的 ISO 字符串，`window` 传 `{}`，`source='user_reported'`，`attribution='not_inferred'`。
- **FR-005** 比率渲染统一走 `fmtRatio()`：`null`/`undefined` → `—`；对象取 `.value` 后 `(v*100).toFixed(1)+'%'`；两者都兼容，因为服务端在"无数据"与"有数据"时返回不同类型的值。
- **FR-006** 所有写操作期间按钮禁用；409/400 展示服务端 detail。
- **FR-007** 不引入 `v-html`、`innerHTML`、内联 `onclick`（既有全局断言覆盖）。
- **FR-008** 新组件控件带 `name` 属性，统计面板容器带 `data-section="recommendation-insights"`，供 Playwright 定位。

## Success Criteria

- **SC-001** 真实浏览器：播种 1 条 accepted + 1 条 pending，登记 outcome（含 1 条指标）→ 刷新回填 → 删除 → 变回未登记；统计面板显示播种数据；runs 表格显示 `no_evidence` 空态。
- **SC-002** `pytest -q` 全绿，新增测试断言 5 条路由的调用点与枚举完整性。
- **SC-003** `npm run build` 成功。
- **SC-004** 真实库 `data/personal_ai_os.db` sha256 与 mtime 不变。
