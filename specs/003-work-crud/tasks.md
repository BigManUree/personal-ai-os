# Tasks: 项目 / 目标 / 任务前端写操作

**Feature**: `003-work-crud` · **Spec**: `spec.md` · **Plan**: `plan.md`

## Phase 1 — 准备

- [x] T001 备份只读版 `Work.vue` 到 `temp/bak-work-vue-readonly.vue`
- [x] T002 核对后端枚举与模型字段（`PROJECT_STATUSES`/`GOAL_KINDS`/`GOAL_STATUSES`/`TASK_STATUSES`/`TASK_PRIORITIES`/`PROJECT_EVENT_TYPES`）

## Phase 2 — 组件

- [x] T003 `frontend/src/components/Modal.vue`：遮罩点击关闭、关闭按钮、`title` prop、默认插槽
- [x] T004 `frontend/src/components/ProjectForm.vue`：5 字段 + 校验 + `saving` 禁用
- [x] T005 `frontend/src/components/GoalForm.vue`：7 字段 + 原生日期控件
- [x] T006 `frontend/src/components/TaskForm.vue`：7 字段 + 项目/目标下拉（按项目联动过滤目标）

## Phase 3 — 页面

- [x] T007 `Work.vue`：`projectBody`/`goalBody`/`taskBody` 全量映射
- [x] T008 `Work.vue`：`Promise.all` 首屏加载 + 失败降级
- [x] T009 `Work.vue`：项目区块（新建/编辑/状态/详情/记事件/删除+确认/状态过滤）
- [x] T010 `Work.vue`：目标区块（新建/编辑/快捷 active→done→dropped/删除+确认/状态过滤）
- [x] T011 `Work.vue`：任务区块（新建/编辑/4 个快捷动作/删除+确认/状态 tab/过期标红）

## Phase 4 — 测试

- [x] T012 `tests/test_work_ui.py`：断言 12 条写路由与三个 Body 函数出现在源码
- [x] T013 `tests/test_work_ui.py`：断言确认文案、枚举选项、无 `v-html`/`onclick`
- [x] T014 后端契约回归：验证「全量 PATCH 保留字段」的真实行为（临时库）
- [x] T015 `pytest -q` 全绿
- [x] T016 `npm run build` 成功且产物被 `index.html` 引用

## Phase 5 — 真实浏览器冒烟

- [x] T017 临时库 + 独立端口启动，Playwright 走完项目/目标/任务全流程
- [x] T018 断言快捷状态切换后 `detail`/`due_date`/`project_id` 未丢失
- [x] T019 真实库未写入校验（头字节 + mtime）

## Phase 6 — 文档交接

- [x] T020 更新 `docs/progress.md`、`docs/current-task.md`、`docs/testing.md`、`docs/decisions.md`、`docs/module-responsibilities.md`
- [x] T021 更新 `README.md` 页面能力说明
- [x] T022 `.specify/feature.json` 指向 003
