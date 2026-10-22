# Implementation Plan: 项目 / 目标 / 任务前端写操作

**Feature**: `003-work-crud` · **Spec**: `specs/003-work-crud/spec.md`

## 技术上下文

- 前端：Vue 3 `<script setup lang="ts">`，无路由库（`App.vue` 用 hash 切页），无状态库，`api()` 是唯一 HTTP 入口。
- 构建：Vite 6 → `app/static/`（`emptyOutDir: true`），产物由 `app/static/index.html` 引用。
- 后端：FastAPI + SQLite，PATCH 为**全量替换**语义。
- 测试：pytest 断言前端源码与构建产物中的关键调用（`frontend_sources` / `frontend_bundle` fixture），Playwright 做真实浏览器冒烟。

## 组件设计

```
frontend/src/
├── components/
│   ├── Modal.vue          通用弹层：遮罩点击 / 关闭按钮 / 标题插槽
│   ├── ProjectForm.vue    项目表单（新建 & 编辑共用）
│   ├── GoalForm.vue       目标表单
│   └── TaskForm.vue       任务表单（项目/目标下拉）
└── pages/
    └── Work.vue           容器：三区块 + 过滤 + 快捷动作 + 详情
```

### 为什么拆 4 个文件

`Work.vue` 若把三个表单全部内联会超过 600 行，且表单复用逻辑（保存中禁用、校验）三次重复。拆分后每个文件 < 250 行，表单可独立演进。不引入 Pinia / Vue Router：页面只有一个，`provide('navigate')` 已够用，新增依赖会扩大构建面。

### 请求体转换（核心）

```ts
// Work.vue
function projectBody(p: any, over: Record<string, any> = {}) { ... }
function goalBody(g: any, over: Record<string, any> = {}) { ... }
function taskBody(t: any, over: Record<string, any> = {}) { ... }
```

三个函数显式列出后端模型的全部字段。所有写请求只允许 `...Body(x, {status: 'done'})` 这种叠加方式。这是对后端全量替换语义的唯一防线，也是测试断言的锚点。

### 状态与刷新策略

| 场景 | 策略 |
| --- | --- |
| 首屏 | `Promise.all([projects, goals, tasks])`，单条失败降级 `[]` |
| 新建 / 编辑 | 成功后 `load()` 全量刷新（保证 `project_name`、计数、排序正确） |
| 快捷状态切换 | 用响应合并到本地行（`{...row, ...updated}`），保留 `project_name`；若当前过滤不再匹配则 `load()` |
| 删除 | 成功后 `load()` |
| 事件追加 | 直接 `unshift` 到详情数组，`event_count += 1` |

快捷切换不刷新全表，是为了避免每次点「完成」都重排列表导致视觉跳动。

### 过滤

三个区块统一在前端 `computed` 过滤。最初计划让目标/任务走后端 `?status=`（接口支持），实现时改为前端过滤，原因：

- 表单里的项目/目标下拉需要**全量**数据，按状态过滤后还得额外维护一份全集，反而更复杂。
- 每次切 tab 都请求会让列表闪烁，并把 `PATCH` 响应缺失的 `project_name` 问题放大。
- 项目接口本身不支持 `status` 参数，三处口径不一致。

代价是数据量大时需要改回服务端过滤 + 分页，已记入 `docs/progress.md` 未开始项。

## 风险与取舍

| 风险 | 处理 |
| --- | --- |
| PATCH 漏字段导致数据被清空 | 三个 `*Body` 函数 + Playwright 专门验证「完成后 detail 未丢」 |
| `PATCH /api/tasks` 返回不含 `project_name` | 合并策略保留旧值；编辑（可能改 project_id）走全量刷新 |
| 删除项目连带删事件 | 二次确认文案明示；不在 UI 提供「强制删除」快捷路径 |
| 空日期传到后端被拒 | 统一 `null`；后端 `_validate_iso_date` 对空串/None 都返回 None |
| 表单重复提交 | `saving` 布尔 + `:disabled`，且提交函数首行短路 |
| 暗色/浅色主题 | 只用既有 `card`/`input`/`btn-*`/`tag` 类，不写死颜色；危险操作使用 `rgb(var(--danger))` |

## 验证计划

1. `pytest -q`（新增 `tests/test_work_ui.py`，断言 12 条写路由 + 三个 Body 函数 + 确认文案）。
2. `npm run build`，确认 bundle 中出现 `/api/tasks`、`/api/goals`、`/api/projects` 的 PATCH 调用。
3. Playwright 冒烟（临时库 + 127.0.0.1 独立端口）：
   - 建项目 → 编辑（改状态，描述保留）→ 记事件
   - 建目标（milestone、target_date）→ 标记完成
   - 建任务（detail/due_date/project_id）→ 完成 → 重开 → 编辑校验字段仍在 → 删除
4. 真实库守卫：`data/personal_ai_os.db` 头字节与 mtime 前后一致。

## 回滚

- 纯前端新增：`git` 未初始化，回滚方式为 `frontend/src/pages/Work.vue` 还原备份 `temp/bak-work-vue-readonly.vue`（实现前先复制），删除 `frontend/src/components/{Modal,ProjectForm,GoalForm,TaskForm}.vue` 与 `tests/test_work_ui.py`，然后 `npm run build` 重建产物。
- 后端零改动。
