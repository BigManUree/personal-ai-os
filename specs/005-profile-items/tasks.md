# Tasks: 个人画像条目管理

**Feature**: `005-profile-items` · **Spec**: `spec.md` · **Plan**: `plan.md`

## Phase 1 — 准备

- [x] T001 备份 `frontend/src/pages/Persona.vue` 到 `temp/bak-persona-readonly.vue`
- [x] T002 核对枚举与状态机（`app/profile_propose.py:32-49`、`app/main.py:2334-2360`）

## Phase 2 — 组件

- [x] T003 `components/ProfileItemForm.vue`：新建/编辑共用，`category` 不含 `inference`，日期控件可清空
- [x] T004 `Persona.vue`：文本画像摘要 + 状态 tab（含「已拒绝」）+ 列表 + `data-item-id` 锚点
- [x] T005 动作矩阵：pending→确认/拒绝；confirmed→过期/编辑/删除；expired→编辑/删除；rejected→说明
- [x] T006 历史展开（按 id 缓存）+ 「生成推测」按钮与依赖说明

## Phase 3 — 测试

- [x] T007 `tests/test_profile_items_ui.py`：9 条路由调用点
- [x] T008 枚举完整性 + `inference` 不在新/编辑 category 下拉 + 动作矩阵断言
- [x] T009 后端契约：rejected PATCH 409、confirm/reject/expire 状态机 409、expire COALESCE、history 结构、propose 400
- [x] T010 `pytest -q` 全绿
- [x] T011 `npm run build` 成功

## Phase 4 — 浏览器冒烟

- [x] T012 播种脚本（1 pending + 1 confirmed + 1 rejected）
- [x] T013 Playwright：确认候选 / 拒绝候选 / 新增条目 / 编辑 / 历史 / propose 400 展示
- [x] T014 真实库未写入校验

## Phase 5 — 文档

- [x] T015 `docs/progress.md`、`docs/current-task.md`、`docs/testing.md`、`docs/decisions.md`、`docs/module-responsibilities.md`
- [x] T016 `README.md` 页面能力
- [x] T017 `.specify/feature.json` → 005
