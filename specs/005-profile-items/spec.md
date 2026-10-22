# Feature Specification: 个人画像条目管理

**Feature Branch**: `005-profile-items`

**Created**: 2026-10-22

**Status**: Implemented

**Input**: 用户指令「按你的建议进行下一步任务并把它下一步做完」。审查报告 E11 的第四项：`profile-items` 家族九条路由前端零调用，`Persona.vue` 只读纯文本画像。

## 背景

`app/main.py:2228-2436` 与 `app/profile_propose.py` 已提供完整的画像条目能力：证据驱动的候选生成、人工确认、过期与修订历史。前端 `Persona.vue`（45 行）只调用 `GET /api/profile`（昵称/目标/偏好三段文本），用户在界面上既看不到候选推测，也无法确认、拒绝、编辑任何画像条目。

## 关键契约（实现必须遵守）

- **状态机在服务端**（`_transition_profile_item(expected,new_status)`）：`confirm`/`reject` 要求当前 `pending`，`expire` 要求当前 `confirmed`，不匹配返回 409（`app/main.py:2334-2341`）。
- **rejected 条目不可编辑**：`PATCH` 命中 rejected 返回 409（`app/main.py:2312`）；expired 可以编辑。
- **列表默认隐藏 rejected**：不传 `status` 时 SQL 追加 `status != 'rejected'`；想看到被拒条目必须显式传 `status=rejected`。
- **手动新增直接是 `confirmed`**（`is_user_stated=1`）：人手输入即陈述事实；`category` 不允许 `inference`，`source_type` 不允许 `model`（`_validate_manual_fields`）。
- **confirm 会把候选"升格为事实"**（`is_user_stated=1`），这是后端注释里明确的语义，UI 必须表达这一点。
- **PATCH 是字段级部分更新**（逐字段 `is not None` 判断），但前端每次提交完整表单；显式传 `valid_from: null` 表示清空日期。
- **`propose` 需要 `LLM_API_KEY`**：未配置返回 400 `未配置 LLM_API_KEY（本地演示模式不生成画像推测）`。
- 内容含密钥样式（`password=...`、`sk-...` 等）会被 `_reject_secret` 拒绝 → 400。

### 枚举（以 `app/profile_propose.py:32-49` 为唯一真源）

- `category`（7）：`basic` / `skill` / `interest` / `goal` / `preference` / `observation` / `inference`；手动新建与编辑只允许前 6 个（`MANUAL_CATEGORIES`）。
- `status`（4）：`pending` / `confirmed` / `expired` / `rejected`。
- `source_type`（手动允许 6）：`manual` / `memory` / `document` / `project` / `goal` / `task`。
- 长度上限：`content` ≤ 2000、`title` ≤ 200、`source_detail` ≤ 500、`meta` ≤ 1000、`confidence` 0.0–1.0。

## User Scenarios & Testing

### User Story 1 - 确认或拒绝候选推测 (Priority: P1)

模型（或播种脚本）产生 `pending` 候选后，用户逐条确认或拒绝。

**Independent Test**: 临时库播种 1 条 `pending`，页面出现候选卡片（含「候选」标记与置信度）；点「确认」后状态变 `confirmed` 且出现「用户确认」标记；对另一条点「拒绝」并内联确认，卡片从默认列表消失，切到「已拒绝」tab 重新出现。

**Acceptance**:

1. `pending` 卡片提供「确认」「拒绝」两个动作；「确认」按钮文案注明"确认后作为个人事实使用"。
2. 「拒绝」先展开内联二次确认；确认后卡片从当前列表消失（服务端默认隐藏 rejected）。
3. 状态不匹配（重复点击、另一端已改动）时展示服务端 409 detail 并刷新列表。
4. `pending` 卡片不可编辑（状态机入口只开放给 pending 的转移动作）；编辑入口仅 `confirmed`/`expired` 提供。

### User Story 2 - 新增与编辑画像条目 (Priority: P1)

用户手动录入一条个人事实，或修改已有条目。

**Independent Test**: 点「新增条目」，填 category=`skill`、content="熟悉 FastAPI 与 SQLite"，提交后列表出现 `confirmed` 条目；点「编辑」改 content，保存后卡片更新；尝试把 category 改成 `inference` 被下拉本身阻止（下拉只有 6 个手动类别）。

**Acceptance**:

1. 表单字段：`category`（6 选下拉，无 `inference`）、`title`（≤200，可空）、`content`（必填 ≤2000）、`confidence`（0–1，默认 1）、`source_type`（6 选，默认 `manual`）、`source_detail`（≤500）、`valid_from`/`valid_until`（原生日期控件，可空）。
2. `content` 为空时本地拦截；长度超限用 `maxlength` 硬约束。
3. 编辑提交 `PATCH`，字段与新建一致；`category` 下拉同样不含 `inference`。
4. 日期清空提交 `null`（后端 `_validate_iso_date` 接受并置 NULL）。
5. 服务端 400（密钥样式、非法枚举）时展示服务端 detail。

### User Story 3 - 过期与删除 (Priority: P2)

已确认条目不再成立时标记过期；彻底作废时删除。

**Acceptance**:

1. `confirmed` 卡片提供「标记过期」，内联确认文案说明"过期日会记为今天（已有过期日则保留）"。
2. `expired` 条目可重新编辑，但不会自动回到 confirmed。
3. 删除需二次确认，文案说明"修订历史会一并删除（服务端留一条删除轨迹）"。

### User Story 4 - 查看修订历史 (Priority: P2)

用户展开某条目的历史，看到创建/确认/修改/过期等事件轨迹。

**Acceptance**:

1. 每张卡片提供「历史」，展开后请求 `GET /api/profile-items/{id}/history`，按时间正序列出 `event`、`detail`、`created_at`。
2. 空历史显示「无修订记录」。
3. 历史请求失败只在该卡片内提示，不影响列表。

### User Story 5 - 生成候选推测 (Priority: P2)

用户触发云端模型基于档案/记忆/目标生成 `pending` 候选。

**Acceptance**:

1. 页面顶部提供「生成推测」按钮；点击后按钮进入加载态。
2. 服务端未配置模型时（400 `未配置 LLM_API_KEY…`），toast 展示该 detail，并在按钮旁固定展示依赖说明，不让用户以为前端勾选能启用模型。
3. 成功后刷新列表；新候选按状态排序自然出现在最前（服务端 pending 优先）。
4. 模型失败（502）展示 `画像生成失败：<异常类型>`。

### Non-goals

- 不改候选生成算法、提示词或置信度规则。
- 不做批量确认/拒绝、跨条目合并。
- 不改 `GET /api/profile` 的纯文本画像展示（保留在页面上方，作为只读摘要）。
- 不做 `meta` 字段的编辑 UI（保持 `{}`，服务端兼容）。

## Functional Requirements

- **FR-001** 新建 `frontend/src/components/ProfileItemForm.vue`：新建与编辑共用；`category` 下拉固定 6 个手动类别（不含 `inference`）。
- **FR-002** `Persona.vue` 重写为容器：状态 tab（默认全部=隐藏 rejected + 显式「已拒绝」入口）、只读文本画像摘要、卡片列表、动作分发、历史展开。
- **FR-003** 所有写操作期间禁用触发按钮；409/400 展示服务端 detail；状态类动作成功后 `load()` 全量刷新。
- **FR-004** 控件带 `name` 属性；列表容器带 `data-section="profile-items"`；卡片带 `:data-item-id`。
- **FR-005** 不引入 `v-html`、`innerHTML`、内联 `onclick`。
- **FR-006** 历史展开状态按条目 id 记录（`Set<number>`），刷新后保留展开态。

## Success Criteria

- **SC-001** 真实浏览器（播种 pending/confirmed 条目）：确认候选 → 状态与「用户确认」标记更新；拒绝候选 → 离开默认列表并在「已拒绝」tab 可见；新增条目 → 出现且为 confirmed；编辑保留改动；历史展示事件轨迹；「生成推测」在无 Key 实例上展示服务端 detail。
- **SC-002** `pytest -q` 全绿，新增测试断言 9 条路由调用点、枚举完整性、`inference` 不出现在新建/编辑下拉、rejected 编辑守卫。
- **SC-003** `npm run build` 成功。
- **SC-004** 真实库 sha256 与 mtime 不变。
