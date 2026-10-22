# Feature Specification: 项目 / 目标 / 任务的前端写操作

**Feature Branch**: `003-work-crud`

**Created**: 2026-10-22

**Status**: Implemented

**Input**: 用户指令「按你的建议进行下一步任务并把它下一步做完」。审查报告 E11 的第二项：`Work.vue` 只有只读列表，后端 12 条 projects/goals/tasks 写路由没有任何调用点。

## 背景

`app/main.py` 已提供完整写接口：

| 资源 | 路由 |
| --- | --- |
| projects | `GET/POST /api/projects`、`GET/PATCH/DELETE /api/projects/{id}`、`POST /api/projects/{id}/events` |
| goals | `GET/POST /api/goals`、`PATCH/DELETE /api/goals/{id}` |
| tasks | `GET/POST /api/tasks`、`PATCH/DELETE /api/tasks/{id}` |

前端 `frontend/src/pages/Work.vue` 只调用了 `GET /api/projects` 与 `GET /api/tasks`，用户无法新建、编辑、改变状态或删除任何条目。后端能力等于不存在。

## 关键契约约束（实现必须遵守）

`PATCH /api/projects/{id}`、`PATCH /api/goals/{id}`、`PATCH /api/tasks/{id}` 复用与 POST 相同的 Pydantic 模型，并执行**全字段 UPDATE**：请求体中省略的字段按模型默认值写入数据库，而不是保持原值。

因此：

- 任何 PATCH 必须提交完整对象；只带 `{"title": ..., "status": "done"}` 会把 `detail` 清空、把 `project_id`/`goal_id` 置为 `NULL`（`app/main.py:1995-2000`）。
- 状态快捷操作（开始 / 完成 / 取消 / 重开）同样必须走完整 body 映射，不能只发状态字段。
- 本特性**不改后端契约**；前端按全量替换语义构造请求。

## User Scenarios & Testing

### User Story 1 - 项目管理 (Priority: P1)

用户在「项目与任务」页新建项目、编辑项目全字段、切换状态、删除项目，并能展开查看该项目的记录事件、目标与任务。

**Independent Test**: 打开页面点「新建项目」，填名称与技术栈提交，列表出现该项目；点「编辑」改名并改状态为 `completed`，卡片状态标签同步变化；展开「详情」看到 0 条事件；点「记事件」写一条 `decision`，详情出现该条；点「删除」出现二次确认，确认后项目消失。

**Acceptance**:

1. 新建表单包含 `name`（必填）、`description`、`tech_stack`、`repo_url`、`status`，`status` 选项与后端 `PROJECT_STATUSES` 完全一致（planning/active/paused/completed/archived）。
2. 名称空白时本地拦截，不发送请求。
3. 编辑提交 `PATCH` 时携带全部 5 个字段；仅改状态不会清空 `description`/`tech_stack`/`repo_url`。
4. 删除前展开内联二次确认，文案明确「项目事件会一并删除，关联的任务与目标保留但会脱离本项目」。
5. 列表支持按状态过滤；过滤为 `completed` 时只显示对应项目。
6. 详情展开后展示 `event_count`/`goal_count`/`open_task_count` 对应的真实条目。

### User Story 2 - 任务管理 (Priority: P1)

用户新建任务（可关联项目与目标）、编辑任务、用快捷按钮推进状态、删除任务。

**Independent Test**: 新建任务时填标题、详情、优先级 `high`、截止日、关联项目；提交后任务出现在列表且显示项目名；点「完成」后状态变 `done`、`completed_at` 非空；重新打开「编辑」，`detail`、`due_date`、`project_id` 仍然存在（证明状态切换没有清空字段）。

**Acceptance**:

1. 任务表单包含 `title`（必填）、`detail`、`status`、`priority`、`due_date`、`project_id`、`goal_id`；下拉数据来自 `GET /api/projects` 与 `GET /api/goals`。
2. 状态快捷动作：`todo→doing`（开始）、`doing→done`（完成）、`done→todo`（重开）、非 `cancelled→cancelled`（取消）。
3. 每个快捷动作发送完整 body；动作完成后不重新拉全表也能更新该行，且 `project_name` 不被抹掉。
4. 列表按状态 tab 过滤（全部 / todo / doing / done / cancelled）；过滤在前端完成（见 FR-008），切 tab 不发请求。
5. 删除任务前二次确认。
6. 优先级用可见标签区分（low/normal/high），过期未完成任务（`due_date` 早于今天且状态非 done/cancelled）标红提示。

### User Story 3 - 目标管理 (Priority: P2)

用户新建目标、编辑目标、标记完成或放弃、删除目标。

**Acceptance**:

1. 目标表单包含 `title`（必填）、`description`、`kind`（long_term/milestone）、`status`（active/done/dropped）、`measure`、`target_date`、`project_id`。
2. `target_date` 使用原生日期控件，格式 `YYYY-MM-DD`，可清空。
3. 删除目标前二次确认，文案明确「关联任务会解除目标关联但保留」。
4. 目标列表按状态过滤。

### User Story 4 - 项目事件记录 (Priority: P2)

用户在项目详情里追加一条事件（progress/decision/issue/learn/note），用于后续建议与检索。

**Acceptance**:

1. 事件类型下拉与 `PROJECT_EVENT_TYPES` 一致。
2. 内容空白时本地拦截；提交成功后详情列表即时插入新事件，`updated_at` 刷新。

### Non-goals

- 不改后端任何路由、模型或校验语义。
- 不做看板拖拽、批量操作、任务排序自定义。
- 不实现跨页面跳转（任务里点项目名跳转项目详情），留待后续。

## Functional Requirements

- **FR-001** 集中定义 `projectBody` / `goalBody` / `taskBody` 三个转换函数，所有 POST/PATCH 必须经它们构造请求体。
- **FR-002** 三个转换函数必须对其他字段（`description`/`detail`/`measure`/`tech_stack`/`repo_url`/`due_date`/`target_date`/`project_id`/`goal_id`）做显式取值，缺省为空串或 `null`。
- **FR-003** 日期字段空值统一传 `null`（后端 `_validate_iso_date` 接受空串与 `None`，统一传 `null` 更明确）。
- **FR-004** 所有写操作期间禁用触发按钮，防重复提交；成功/失败均用 toast 展示，失败展示服务端 `detail`。
- **FR-005** 表单在弹层内呈现，`Esc`/点击遮罩/「关闭」均可退出；关闭不提交。
- **FR-006** 页面加载用 `Promise.all` 并行拉取 projects / goals / tasks，任一失败单独降级为空数组并提示。
- **FR-007** 不引入 `v-html`、`innerHTML` 或内联 `onclick`（既有安全断言 `tests/test_recommendation_feedback_api.py:342-345` 覆盖整个 `frontend/src`）。
- **FR-008** 首屏一次拉全量 projects/goals/tasks，状态过滤在前端 `computed` 完成；理由见 `plan.md`「为什么不用服务端过滤」。表单内的项目/目标下拉依赖全量数据。
- **FR-009** 表单控件必须带 `name` 属性，三个列表区块必须带 `data-section="projects|goals|tasks"`，供 Playwright 稳定定位。

## 数据与接口映射

| 前端动作 | 请求 |
| --- | --- |
| 新建项目 | `POST /api/projects` + `projectBody` |
| 编辑项目 | `PATCH /api/projects/{id}` + `projectBody` |
| 项目状态变更 | `PATCH /api/projects/{id}` + `projectBody({status})` |
| 删除项目 | `DELETE /api/projects/{id}` |
| 记录事件 | `POST /api/projects/{id}/events` `{event_type, content}` |
| 新建/编辑目标 | `POST /api/goals` / `PATCH /api/goals/{id}` + `goalBody` |
| 删除目标 | `DELETE /api/goals/{id}` |
| 新建/编辑任务 | `POST /api/tasks` / `PATCH /api/tasks/{id}` + `taskBody` |
| 任务状态快捷动作 | `PATCH /api/tasks/{id}` + `taskBody({status})` |
| 删除任务 | `DELETE /api/tasks/{id}` |

## Success Criteria

- **SC-001** 真实浏览器（Playwright，临时库）完成：建项目 → 编辑项目（保留描述）→ 记事件 → 建目标 → 建任务（带 detail/due_date/project_id）→ 完成 → 重开 → 验证字段未丢 → 删除三者，全部通过。
- **SC-002** `pytest -q` 全绿，且新增测试断言前端确实调用了 12 条写路由中的每一条。
- **SC-003** `npm run build` 成功，`app/static` 产物被引用。
- **SC-004** 真实开发库 `data/personal_ai_os.db` 未被写入（头字节与 mtime 不变）。
