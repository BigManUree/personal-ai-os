# Tasks: 建议结果登记与统计洞察

**Feature**: `004-recommendation-insights` · **Spec**: `spec.md` · **Plan**: `plan.md`

## Phase 1 — 准备

- [x] T001 备份 `frontend/src/pages/Recommendations.vue` 到 `temp/bak-recommendations-vue-002.vue`
- [x] T002 核对枚举：`EXECUTION_STATUSES`/`RESULT_RATINGS`/`OBSERVATION_METRICS`/`OBSERVATION_UNITS`（`app/recommendation_feedback.py:29-59`）

## Phase 2 — 结果登记

- [x] T003 `components/OutcomeForm.vue`：GET 回填、PUT 保存、DELETE 删除、409/400 处理
- [x] T004 指标观察编辑器：增删行、上限 10、空行过滤、`captured_at` 自动生成
- [x] T005 过期禁用 + 未选执行状态本地拦截

## Phase 3 — 统计与历史

- [x] T006 `components/RecommendationInsights.vue`：statistics + runs 并行加载、各自降级
- [x] T007 窗口 7/30/90 天 + `fmtRatio`（null → `—`）
- [x] T008 指标对比表 + `limitations` 原文 + `sample_status` 提示
- [x] T009 runs 表格（10 列，error 截断 200 字符）

## Phase 4 — 接入

- [x] T010 `Recommendations.vue`：import、顶部「统计与历史」按钮、accepted 卡片挂载 OutcomeForm
- [x] T011 构建并确认产物被 `index.html` 引用

## Phase 5 — 测试

- [x] T012 `tests/test_recommendation_insights_ui.py`：5 条路由调用点
- [x] T013 枚举完整性 + `fmtRatio` 空值分支 + 无 `v-html`/`innerHTML`/`onclick`
- [x] T014 后端契约回归：PUT 部分更新语义、首次缺 `execution_status` 失败、非 accepted 409
- [x] T015 `pytest -q` 全绿

## Phase 6 — 浏览器冒烟

- [x] T016 播种脚本（1 条 accepted + 1 条 pending）
- [x] T017 Playwright：登记 → 回填 → 删除；统计数字；runs 表格
- [x] T018 真实库未写入校验

## Phase 7 — 文档

- [x] T019 `docs/progress.md`、`docs/current-task.md`、`docs/testing.md`、`docs/decisions.md`、`docs/module-responsibilities.md`
- [x] T020 `README.md` 页面能力
- [x] T021 `.specify/feature.json` → 004
