# Tasks: 建议采纳与生成入口

**Feature**: `002-recommendation-actions` · **Input**: [spec.md](./spec.md) / [plan.md](./plan.md)

## Phase 1 — 列表与筛选

- [x] T001 读取后端契约：`app/recommendations.py` 的 `TRANSITIONS`、`STATUSES`、`DEFAULT_HIDDEN_STATUSES`，确认四个动作的前置状态
- [x] T002 `Recommendations.vue` 增加状态标签（进行中/待处理/已采纳/已忽略/已拒绝/已过期），`statusFilter` 驱动 `?status=`
- [x] T003 空结果按筛选给不同文案；标题区显示总数

## Phase 2 — 状态动作

- [x] T004 `act(rec, action)` 调用 `/api/recommendations/{id}/{action}`，`busyId` 防重复提交
- [x] T005 pending 卡片：采纳 / 忽略 / 拒绝（内联二次确认，提示 30 天冷却）
- [x] T006 accepted 卡片：撤回采纳；终态卡片显示说明文本
- [x] T007 失败路径：展示后端 `detail` 并重新加载列表（404/409 场景）
- [x] T008 动作后局部合并响应；不在当前筛选时整表刷新

## Phase 3 — 生成入口

- [x] T009 折叠面板：窗口 7/14/30、`use_model` 勾选、服务端模型门槛说明
- [x] T010 生成结果摘要：状态、窗口、`gaps`、`model_disabled_by_server`；面板保持展开
- [x] T011 生成成功后回到默认视图并刷新

## Phase 4 — 性能与安全

- [x] T012 反馈数据改为 `Promise.all` 并行拉取（消除 N+1）
- [x] T013 证据列表用 `details/summary` 渲染，文本走 Vue 插值（无 `v-html`）

## Phase 5 — 验证

- [x] T014 `npm run build`（`emptyOutDir: true`），确认 `app/static` 仅 `index.html` + 1 JS + 1 CSS
- [x] T015 `tests/test_recommendations_api.py` 增补接线断言：`/generate` 在产物、四个动作在组件源码、`撤回采纳`/`确认拒绝` 文案存在
- [x] T016 `compileall` + `pytest -q` → `488 passed, 1 skipped, 1 warning`
- [x] T017 Playwright 交互冒烟（临时库 + 127.0.0.1:18766）：11/11 通过
- [x] T018 Playwright 生成冒烟：4/4 通过（`no_evidence` 场景下仍展示窗口与状态）

## Phase 6 — 收尾

- [x] T019 `docs/progress.md`、`docs/testing.md` 记录本批结果与未覆盖项
- [ ] T020 下一特性：outcome 登记面板 + 建议统计面板（原 `recommendations.js` 的能力缺口）
