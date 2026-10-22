# Feature Specification: 建议采纳与生成入口

**Feature Branch**: `002-recommendation-actions`

**Created**: 2026-10-22

**Status**: Implemented

**Input**: 用户指令「按你的建议开始下一步任务」——审查报告 E11 的第一项：前端补齐建议状态机（采纳/忽略/拒绝/撤回）与生成入口。

## 背景

后端 `app/recommendations.py` 与 `app/main.py` 已提供完整状态机与生成接口，但 Vue 前端只剩列表与反馈打分：`POST /api/recommendations/{id}/{accept|ignore|reject|reopen}` 与 `POST /api/recommendations/generate` 没有任何调用点。结果是用户在界面上无法处置建议，也无法在没有建议时生成建议。

## User Scenarios & Testing

### User Story 1 - 处置一条待处理建议 (Priority: P1)

用户在「智能建议」页看到一条 `pending` 建议，可以直接采纳、忽略或拒绝；拒绝需要二次确认，因为服务端有 30 天同类冷却。

**Independent Test**: 浏览器打开已有一条 `pending` 建议的实例，点击「采纳」，卡片状态变为 `accepted` 并出现「撤回采纳」；点击「撤回采纳」回到 `pending`；点击「拒绝」出现「确认拒绝」与「取消」，取消后状态不变。

**Acceptance**:

1. `pending` 卡片提供 采纳 / 忽略 / 拒绝 三个动作，动作进行中按钮禁用，防重复提交。
2. 「拒绝」先展开内联确认（提示 30 天冷却），确认后才发请求。
3. `accepted` 卡片提供「撤回采纳」。
4. 已是终态（ignored/rejected/expired）的卡片只显示说明文本，不给动作。
5. 请求失败（404/409）展示服务端 `detail`，并重新拉取列表以呈现真实状态。

### User Story 2 - 按状态查看建议 (Priority: P2)

用户需要在「进行中 / 待处理 / 已采纳 / 已忽略 / 已拒绝 / 已过期」之间切换。

**Independent Test**: 点击标签后请求带 `?status=`；「进行中」不带参数，沿用服务端默认隐藏 rejected/expired 的语义。

**Acceptance**:

1. 标签选中态可见，切换后重新加载列表。
2. 空结果按当前筛选给出不同文案。
3. 处置后若该条不再属于当前筛选，列表自动刷新。

### User Story 3 - 生成建议 (Priority: P1)

用户在没有任何建议时能自己触发一次生成。

**Acceptance**:

1. 折叠面板提供统计窗口（7/14/30 天，与服务端 `SUPPORTED_WINDOWS` 一致）与「允许模型润色」。
2. 面板明示模型需服务端 `LLM_API_KEY` + `RECOMMENDATIONS_USE_MODEL=1` 才能生效。
3. 生成完成后面板保持展开，显示状态、窗口范围、数据缺口；`model_disabled_by_server` 时给出说明。
4. 生成后列表刷新到默认视图。

### Non-goals

- 不实现"执行结果登记"（outcome）面板与建议统计面板，留待下一特性。
- 不改后端状态机、冷却规则或生成算法。

## Functional Requirements

- **FR-001**: 前端必须调用现有的 `accept/ignore/reject/reopen` 四个接口，不新增后端路由。
- **FR-002**: 动作失败必须呈现后端错误消息，并刷新列表（禁止静默）。
- **FR-003**: 列表加载的反馈请求必须并行（原实现为逐条串行，N 条建议 N 次往返）。
- **FR-004**: 生成面板的窗口取值必须来自服务端支持集合，客户端不得传入任意值。
- **FR-005**: 页面不得使用 `v-html`/`innerHTML` 渲染建议内容或证据。

## Success Criteria

- **SC-001**: 浏览器冒烟 11/11 通过：挂载、路由、卡片、证据摘要、采纳、撤回、拒绝二次确认、取消、无控制台错误。
- **SC-002**: 生成冒烟 4/4 通过：面板展开、结果摘要、可重复生成、无 5xx。
- **SC-003**: `pytest -q` 保持 `488 passed, 1 skipped`；前端断言覆盖新接线。
- **SC-004**: `app/static` 仅含 `index.html` + 1 JS + 1 CSS（`emptyOutDir` 生效）。
