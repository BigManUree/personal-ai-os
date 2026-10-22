# Personal AI OS 1.3.4 建议反馈与结果追踪 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在不自动执行建议、不自动改写长期记忆或个人画像的前提下，建立可追溯的建议反馈、采纳后执行结果和质量统计能力。

**Architecture:** 1.3.4 复用现有 `recommendations` 建议表、状态机、行为事件与确定性统计模块。反馈和结果使用独立表保存显式用户输入；缺失反馈始终按“未知”处理，不推断为不满意；效果数据只记录观察与用户登记，不把指标改善自动归因于建议。

**Tech Stack:** Python 3、FastAPI、SQLite、Pydantic、pytest、原生 HTML/CSS/JavaScript；不引入新依赖，不调用大模型。

---

## 0. 本文件对应的批次

- **1.3.4-A：现状审计与数据模型设计** —— 本批只新增/更新计划文档，不修改业务代码，不新增数据库表，不执行迁移。
- 1.3.4-B：反馈与结果服务层。
- 1.3.4-C：反馈 API 与智能建议页面入口。
- 1.3.4-D：效果统计与质量评估。
- 1.3.4-E：安全、并发、隐私与完整回归验收。

未经明确批准，不得开始 B 批。

## 1. 2026-10-12 现状审计（A 批）

### 1.1 本轮实际检查

- 读取 `CLAUDE.md`、`README.md`、`docs/plans/phase-4-personalization-engine.md`、`docs/plans/phase-4-1.3.3-personalized-recommendations.md`。
- 读取并核对：
  - `app/recommendations.py`
  - `app/recommendation_engine.py`
  - `app/analytics.py`
  - `app/behavior_events.py`
  - `app/main.py`
  - `app/static/recommendations.js`
  - `tests/test_recommendations_store.py`
  - `tests/test_recommendation_engine.py`
  - `tests/test_recommendations_api.py`
  - `tests/test_behavior_events.py`
  - `tests/test_analytics.py`
- 使用只读 SQLite URI 检查 `data/personal_ai_os.db`：存在 `recommendations`、`recommendation_runs`、`behavior_events`，不存在反馈或结果表。
- 确认当前目录不是 Git 仓库；未初始化 Git、未提交、未推送。
- 本轮重新运行基线测试，而不是沿用历史口头结果。

### 1.2 当前基线测试

命令：

```powershell
$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest -q
```

本轮实测结果：

```text
251 passed, 1 skipped, 5 warnings in 71.29s
```

5 个 warning 均为既有 FastAPI `on_event` / TestClient `httpx` 弃用提示，本批不处理。

### 1.3 已有能力

| 能力 | 当前实现 | 1.3.4 影响 |
| --- | --- | --- |
| 建议主表 | `recommendations`：标题、正文、类别、优先级、状态、证据、窗口、覆盖率、预期收益、风险、置信度、规则版本、去重键、过期时间 | 反馈和结果通过 `recommendation_id` 关联，不把反馈字段塞进建议表 |
| 建议状态 | `pending/accepted/ignored/rejected/expired`；支持 accept、ignore、reject、accepted→pending reopen | 保留现有状态机；采纳仍不等于执行 |
| 运行记录 | `recommendation_runs`：候选、新建、重复、冷却抑制、过期、模型降级和脱敏错误 | 不保存用户反馈正文；质量统计另走反馈/结果表 |
| 行为事件 | `behavior_events` 保存最小元数据，不保存标题、正文或文档内容 | B 批可扩展推荐相关事件类型，但仍不得保存自由文本或秘密 |
| 确定性统计 | `app/analytics.py` 区分 `no_data/insufficient_data/ok`，按上海时区窗口统计 | D 批复用统计口径，不能把无反馈计成负反馈 |
| 建议页面 | 已有列表、详情、生成、采纳/忽略/拒绝/撤销采纳、运行记录 | C 批在现有页面增加反馈与结果入口，不重写 UI |
| 安全边界 | 本地 loopback、Same-Origin/Host 校验、模型默认关闭、输入长度限制和脱敏 | 继续适用；系统仍不是公网多用户产品 |

### 1.4 明确缺口

1. 数据库中没有建议反馈、有用性评价、执行结果或预期/实际对比表。
2. `behavior_events` 当前没有 recommendation 相关事件类型；已有历史建议的完整状态轨迹不可还原。
3. `recommendations.status='accepted'` 只表示用户接受建议，不能证明任务已执行或产生效果。
4. 现有 `reviewed_at/updated_at` 只反映最近一次状态变化，不能代替反馈历史。
5. 没有用户主动登记结果的入口；不能由模型或系统自动推断“已完成/无效”。
6. 没有反馈分布、采纳后完成率、预期/实际差异的统计口径。

## 2. 核心语义定义（先于代码固定）

### 2.1 采纳、反馈、执行结果三者分离

- **采纳（accepted）**：用户愿意接受或考虑这条建议；不代表系统已执行，也不代表用户已完成。
- **反馈（feedback）**：用户对建议质量的主观评价，例如是否有用、原因是什么；反馈不要求必须采纳。
- **执行结果（outcome）**：用户在采纳后主动登记的实际执行情况和效果观察。
- **统计指标**：程序按显式记录计算；无反馈、无结果均为“未知/缺失”，不是负反馈。
- **因果关系**：即使某指标随后改善，也只能记录为同时间窗口的观察，不能自动证明由该建议导致。

### 2.2 临时选择与长期偏好

- 用户拒绝一条建议可能只是“现在不适合”，不能自动推导为永久偏好。
- 反馈表用 `preference_scope` 记录用户明确选择：`temporary`、`persistent`、`unspecified`。
- 临时偏好可带 `resume_after_at`；长期偏好也只是本产品内的推荐偏好，不自动写入 `memories` 或 `profile_items`。
- 1.3.4 只采集和展示该信号；是否让生成器调整冷却或排序，应在后续经批准后实施。

## 3. 1.3.4-A 数据模型设计

### 3.1 新增表总览

| 表 | 关系 | 用途 |
| --- | --- | --- |
| `recommendation_feedback` | 一条建议最多一条当前反馈 | 保存有用/无用评价、原因、备注和临时/长期范围 |
| `recommendation_outcomes` | 一条建议最多一条当前结果 | 保存采纳后的执行状态、实际效果、用户登记时间和指标观察 |

不新增反馈历史表或结果历史表：第一版使用唯一当前记录 + `created_at/updated_at`；需要多次纵向复盘时另立后续迭代。行为事件只记录状态变化和枚举值，不保存备注正文。

### 3.2 `recommendation_feedback`

建议 SQL（B 批实施时放入现有 `init_db()`，使用 `CREATE TABLE IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER NOT NULL UNIQUE,
    vote TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    preference_scope TEXT NOT NULL DEFAULT 'unspecified',
    resume_after_at TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (vote IN ('helpful', 'unhelpful')),
    CHECK (preference_scope IN ('temporary', 'persistent', 'unspecified')),
    CHECK (source IN ('user')),
    CHECK (length(note) <= 1000),
    CHECK (length(reason_codes_json) <= 3000),
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_vote
    ON recommendation_feedback(vote, updated_at);
CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_scope
    ON recommendation_feedback(preference_scope, resume_after_at);
```

字段说明：

- `vote`
  - `helpful`：有用。
  - `unhelpful`：无用或不适用。
- `reason_codes_json`：受限原因码数组，最多 5 个；不允许任意 key/value。
- `note`：用户可选备注，最多 1000 字；经过截断、类型校验和秘密样式过滤。
- `preference_scope`
  - `temporary`：用户明确表示只是暂时不适合。
  - `persistent`：用户明确表示长期不希望看到同类建议。
  - `unspecified`：未声明，默认值，不能由模型推断。
- `resume_after_at`：仅当 `preference_scope='temporary'` 时可设置；必须是未来时间。
- `source`：第一版只允许 `user`；不提供 model/system 自动反馈。
- 时间统一保存 UTC ISO-8601 字符串；页面按本地时区展示。

#### 允许的原因码

`vote='helpful'`：

- `actionable`：可执行。
- `relevant`：与我的情况相关。
- `evidence_clear`：依据清楚。
- `timely`：提醒及时。
- `realistic`：现实可行。
- `other`：其他。

`vote='unhelpful'`：

- `irrelevant`：不相关。
- `too_obvious`：过于常识化。
- `unrealistic`：不可执行。
- `outdated`：信息过期。
- `wrong_evidence`：依据不准确。
- `too_risky`：风险过高。
- `not_now`：暂时不需要。
- `duplicate`：重复建议。
- `other`：其他。

服务层必须拒绝与 `vote` 不匹配的原因码。

### 3.3 `recommendation_outcomes`

```sql
CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER NOT NULL UNIQUE,
    execution_status TEXT NOT NULL,
    result_rating TEXT NOT NULL DEFAULT 'unclear',
    actual_effect_note TEXT NOT NULL DEFAULT '',
    metric_observations_json TEXT NOT NULL DEFAULT '[]',
    causality_status TEXT NOT NULL DEFAULT 'not_inferred',
    occurred_at TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (execution_status IN (
        'planned', 'completed', 'partially_completed',
        'not_completed', 'no_longer_relevant'
    )),
    CHECK (result_rating IN (
        'much_better', 'better', 'as_expected',
        'worse', 'much_worse', 'unclear'
    )),
    CHECK (causality_status IN ('not_inferred')),
    CHECK (source IN ('user')),
    CHECK (length(actual_effect_note) <= 1000),
    CHECK (length(metric_observations_json) <= 3000),
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_execution
    ON recommendation_outcomes(execution_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_rating
    ON recommendation_outcomes(result_rating, updated_at);
```

字段说明：

- `execution_status`
  - `planned`：已采纳并计划执行，但尚无结果。
  - `completed`：用户登记已完成。
  - `partially_completed`：只完成一部分。
  - `not_completed`：未完成。
  - `no_longer_relevant`：情况变化，建议不再适用。
- `result_rating`
  - `much_better/better/as_expected/worse/much_worse/unclear`。
  - 这是用户对实际效果与预期收益的比较，不是程序计算出的因果结论。
- `actual_effect_note`：用户主动填写的结果说明，最多 1000 字。
- `metric_observations_json`：最多 10 条受限指标观察，第一版可为空数组。
- `causality_status`：第一版固定 `not_inferred`，明确“不推断因果”。
- `occurred_at`：结果发生时间；`planned` 允许为空，终态结果要求用户提供或由服务端取当前时间。
- `source`：第一版固定 `user`；系统观测和模型推测均不得写入。

`metric_observations_json` 中每条数据只允许以下形状：

```json
{
  "metric": "tasks_completed",
  "expected_value": null,
  "observed_value": 3,
  "unit": "count",
  "window": {"days": 7, "timezone": "Asia/Shanghai", "start": "...", "end": "..."},
  "source": "user_reported",
  "captured_at": "2026-10-12T09:00:00+00:00",
  "attribution": "not_inferred"
}
```

B 批先完成枚举、标量类型、长度、指标名白名单和 JSON 大小校验；C 批页面第一版可以只暴露执行状态、效果评级、结果时间和备注，不要求用户手填复杂 JSON。D 批如加入统计快照，也必须保留 `attribution='not_inferred'`。

### 3.4 迁移与旧数据兼容

- 只新增两张表和索引，不 `ALTER` 现有 `recommendations` 表。
- 迁移使用现有 `init_db()` 的幂等模式；旧库重复启动不会重复建表。
- 不从历史 `accepted/rejected/ignored` 状态回填反馈，因为这些状态没有用户评价原因，也不能证明实际结果。
- 不回填历史 recommendation 行为事件；旧建议的完整生命周期已经无法从当前单行状态可靠还原。
- 删除建议时依赖 SQLite foreign key + `PRAGMA foreign_keys=ON` 级联删除其反馈和结果。
- B 批必须用模拟旧库验证：已有建议、运行记录、行为事件、文档、任务等数据在迁移后仍可查询。

## 4. 服务层计划（1.3.4-B）

新增 `app/recommendation_feedback.py`，不与现有 `app/recommendations.py` 混杂。

### 4.1 Pydantic 模型

- `FeedbackDraft`
  - `vote`
  - `reason_codes`
  - `note`
  - `preference_scope`
  - `resume_after_at`
- `OutcomeDraft`
  - `execution_status`
  - `result_rating`
  - `actual_effect_note`
  - `metric_observations`
  - `occurred_at`

复用 `app.profile_propose.looks_like_secret` 或项目现有脱敏工具；拒绝 API Key、token、密码样式内容。

### 4.2 服务函数

计划提供：

```python
def get_feedback(conn, recommendation_id: int) -> dict | None
def upsert_feedback(conn, recommendation_id: int, data: FeedbackDraft | dict, *, now=None) -> dict
def delete_feedback(conn, recommendation_id: int) -> None
def get_outcome(conn, recommendation_id: int) -> dict | None
def upsert_outcome(conn, recommendation_id: int, data: OutcomeDraft | dict, *, now=None) -> dict
def delete_outcome(conn, recommendation_id: int) -> None
```

错误继续使用与建议存储层一致的服务错误风格：

- 建议不存在：404。
- 在未采纳建议上创建/修改结果：409。
- 建议已逻辑过期或状态不允许结果更新：409。
- 枚举、时间、JSON、长度或秘密内容不合法：400。
- 并发唯一键冲突：重新读取赢家；无法安全重放时返回 409。

### 4.3 生命周期约束

- 反馈可针对存在的建议创建；不强制要求建议为 accepted。
- 结果只允许在建议当前为 `accepted` 时创建或更新。
- `accepted -> pending` 撤销采纳后，已有结果保留用于历史查看，但不能继续更新；重新采纳后可更新。
- 建议为 `ignored/rejected/expired` 时不能新建结果。
- 反馈或结果不得修改建议状态、任务、项目、目标、记忆或画像。
- 所有写操作与调用方共享同一个 SQLite 事务；业务写入失败时整体回滚。

### 4.4 推荐相关行为事件

B 批扩展 `app/behavior_events.py` 常量：

- 事件类型：
  - `recommendation.accepted`
  - `recommendation.ignored`
  - `recommendation.rejected`
  - `recommendation.reopened`
  - `recommendation.feedback_saved`
  - `recommendation.feedback_deleted`
  - `recommendation.outcome_saved`
- 主体类型：
  - `recommendation`
  - `recommendation_feedback`
  - `recommendation_outcome`

事件 metadata 只允许枚举和计数，例如 `vote`、`reason_count`、`preference_scope`、`execution_status`、`result_rating`，不得保存备注、指标窗口外的内容或任何自由文本。

是否把这些事件直接加入现有 `transition_status()`，应由 B 批以最小改动实现并测试；不得改变 1.3.3 既有状态转换语义。若直接在存储层记录事件会造成循环依赖，则在现有 API 事务调用处记录，但必须保证同一事务回滚。

## 5. API 与页面计划（1.3.4-C）

### 5.1 API（具体路径可按现有规范微调）

- `GET /api/recommendations/{rec_id}/feedback`：获取当前反馈；无记录返回 `null` 或项目一致的空对象。
- `PUT /api/recommendations/{rec_id}/feedback`：创建或更新反馈。
- `DELETE /api/recommendations/{rec_id}/feedback`：删除反馈。
- `GET /api/recommendations/{rec_id}/outcome`：获取当前结果。
- `PUT /api/recommendations/{rec_id}/outcome`：创建或更新结果。
- `DELETE /api/recommendations/{rec_id}/outcome`：删除结果。

所有写接口继续经过现有 Host/Same-Origin 保护；GET 不得隐式写入。

### 5.2 前端页面

在现有「智能建议」详情或卡片操作区增加：

- 有用/无用评价按钮。
- 原因码选择。
- 可选备注输入。
- 临时/长期偏好选择；临时偏好可选择恢复时间。
- 采纳后显示“登记执行结果”入口。
- 执行状态、效果评级、结果时间、实际效果说明。
- 保存前展示确认文案：将保存哪些内容、不会自动执行什么、不会写入长期记忆。
- 加载、空状态、400/404/409 错误、保存中防重复提交和成功后局部刷新。
- 所有正文、备注、错误继续使用 `textContent`/`esc()`，禁止不安全 `innerHTML` 拼接。

## 6. 质量统计计划（1.3.4-D）

新增独立服务或在 `app/analytics.py` 中以清晰边界扩展，不改动已有 1.3.2 返回契约。

建议新函数：`recommendation_quality(conn, *, days, timezone_name, now=None)`。

### 6.1 指标口径

- `generated_total`：窗口内新建建议数。
- `status_counts`：当前各状态建议数。
- `feedback_count`：有显式反馈的建议数。
- `feedback_missing_count`：无反馈建议数；语义为未知，不是不满意。
- `helpful_count/unhelpful_count`：仅以反馈表显式记录为分母来源。
- `helpful_rate`：`helpful / (helpful + unhelpful)`；无反馈时为 `no_data`，不是 0。
- `accepted_count`：第一版可使用当前 `accepted` 状态；若 B 批生命周期事件完成，可同时提供 `ever_accepted_count` 并说明覆盖率。
- `outcome_count`：有结果登记的建议数。
- `completed_count`：`execution_status='completed'`。
- `partially_completed_count`：`execution_status='partially_completed'`。
- `not_completed_count`：`execution_status='not_completed'`。
- `followthrough_rate`：完成或部分完成数 / 已采纳建议数；无结果采纳者进入缺失，不按未完成处理。
- `result_rating_distribution`：效果评级分布；分母为有结果且评级非 `unclear` 的记录。
- `temporary_preference_count/persistent_preference_count/unspecified_preference_count`：反馈偏好分布。

所有比率指标继续使用 1.3.2 的结构：`value/status/sample_size/definition_version`，样本不足时返回 `insufficient_data` 或 `no_data`。

### 6.2 禁止的统计推断

- 不能把未反馈计成无用。
- 不能把未登记结果计成未完成。
- 不能把指标变化归因于建议。
- 不能根据一次拒绝推断人格、动机或稳定习惯。
- 不能把反馈自动写入长期记忆或个人画像。

## 7. 分批任务与验收标准

### A：现状审计与数据模型（本批）

范围：文档和审计。

验收：

- 确认无反馈/结果表。
- 给出兼容旧库的表结构、枚举、生命周期和迁移规则。
- 基线测试已复跑。
- 未修改业务代码。

状态：**本批完成后停止，等待 B 批准。**

### B：领域模型与存储服务

修改范围（预计）：

- 修改：`app/main.py`（仅 `init_db()` 新表/索引）。
- 新增：`app/recommendation_feedback.py`。
- 修改：`app/behavior_events.py`（仅新增常量与白名单）。
- 新增：`tests/test_recommendation_feedback_store.py`。
- 可能修改：现有建议状态相关测试，只用于验证同事务事件，不改弱断言。

验收：

- 新旧数据库均可幂等迁移。
- 反馈/结果 CRUD/upsert、唯一约束、级联删除、非法输入、404/409、并发更新均有测试。
- 备注和 JSON 中的秘密样式被拒绝。
- 不写任务、项目、目标、记忆或画像。
- 建议专项和全量测试通过。

### C：API 与页面

修改范围（预计）：

- 修改：`app/main.py`、`app/static/index.html`、`app/static/recommendations.js`。
- 新增/修改：`tests/test_recommendations_api.py` 或独立反馈 API 测试。
- 前端 JS 语法检查和临时库浏览器冒烟。

验收：

- API 参数、状态码、分页/404/409、CSRF/Host 约束通过。
- 页面能评价、登记结果、修改、删除并展示错误/空状态。
- 保存期间防重复提交。
- 不提供任何“自动执行建议”按钮。

### D：质量统计

修改范围（预计）：

- 新增：`app/recommendation_quality.py` 或在分析模块中清晰扩展。
- API 与页面仅做只读展示。
- 新增统计测试。

验收：

- 零值、缺失、样本不足语义明确。
- 采纳率、反馈分布、结果完成率、评级分布有确定性公式和定义版本。
- 统计窗口和时区边界有测试。
- 不做因果归因，不自动调整生成策略。

### E：安全与质量验收

范围：独立审计、边界测试和最小修复，不增加功能。

重点：

- 级联删除与误删隔离。
- 并发 upsert 和状态竞争。
- XSS、提示注入、超长输入、秘密文本。
- 旧库迁移和现有 251 项测试回归。
- 浏览器冒烟。
- 文档更新和风险清单。

## 8. 明确不做

1.3.4 全阶段默认不做：

- 不自动执行建议。
- 不自动创建、修改或完成任务。
- 不自动修改长期记忆、个人画像或推荐生成规则。
- 不调用大模型解释反馈结果。
- 不接入外部问卷、第三方分析或远程上报。
- 不引入多用户登录；当前仍按单用户本地 loopback 应用设计。
- 不把相关性包装成因果关系。
- 不实现复杂奖励模型、强化学习或分布式任务系统。

## 9. A 批交付结论

- 已确认 1.3.3 A/B/C/D/E 收口后的代码基线。
- 已确认当前没有反馈与结果数据表。
- 已设计两张独立新表、枚举、唯一约束、级联删除和旧库兼容策略。
- 已定义反馈、采纳、执行结果、缺失数据、临时/长期偏好和因果边界。
- 已制定 B/C/D/E 的实施范围和验收标准。
- 本批没有实现业务功能、没有迁移数据库、没有调用模型或外部服务。


## 10. 1.3.4-B 实施记录（2026-10-12）

### 10.1 实际交付

- 新增独立服务模块 `app/recommendation_feedback.py`：
  - `FeedbackDraft`、`MetricObservation`、`OutcomeDraft`；
  - `get_feedback()`、`upsert_feedback()`、`delete_feedback()`；
  - `get_outcome()`、`upsert_outcome()`、`delete_outcome()`；
  - 服务层错误复用 `RecommendationError`（别名 `FeedbackError`），携带 400/404/409/500 状态提示。
- 在 `app/main.py` 的既有 `init_db()` 中只新增两张表和索引：
  - `recommendation_feedback`；
  - `recommendation_outcomes`。
- 扩展 `app/behavior_events.py` 的推荐反馈/结果与建议状态事件白名单；未保存反馈备注、建议标题、正文或其他自由文本。
- `app/recommendations.py` 的建议状态动作在同一事务中记录最小行为事件：accepted、ignored、rejected、reopened。
- 新增 `tests/test_recommendation_feedback_store.py`，覆盖初始化、CRUD/upsert、一对一唯一约束、枚举、长度、时间、秘密样式文本、指标观察、级联删除、事务回滚、并发创建、旧库迁移和真实数据边界。

### 10.2 数据一致性语义

- 两张新表均通过 `recommendation_id INTEGER NOT NULL UNIQUE` 保证一条建议最多一条当前反馈/结果。
- 外键为 `FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE`。
- 服务写入前执行 `PRAGMA foreign_keys` 检查；未启用外键时拒绝写入，避免在连接配置错误时产生孤立记录。
- 反馈和结果更新采用“读取当前记录 → 合并显式输入 → Pydantic 校验 → 全字段写回”的当前记录 upsert；这是可修改的当前记录，不是历史审计表。
- 结果只允许在建议当前状态为 `accepted` 且未逻辑过期时创建/更新；撤销采纳后已有结果保留但不能继续修改。
- `causality_status` 不由调用方提供，服务层始终写为 `not_inferred`。
- `planned` 结果不写 `occurred_at`；终态结果未提供时间时使用服务端当前时间；未来结果时间被拒绝。
- 指标观察仅允许白名单指标、单位、来源、有限数值和受约束 window；不允许额外字段，`attribution` 必须为 `not_inferred`。

### 10.3 事务与并发

- 反馈/结果写入、行为事件写入和调用方业务操作共享同一个 SQLite 连接与事务；失败时由调用方回滚，测试验证反馈与行为事件均不会半写入。
- 数据库唯一约束兜底并发创建；竞争失败方转换为 `FeedbackError(code="conflict", status_code=409)`。
- 删除建议时由已启用的 SQLite 外键级联直接删除对应反馈和结果；已在临时数据库实测。

### 10.4 本批明确未做

- 未新增 HTTP API。
- 未修改前端页面或 JavaScript。
- 未实现效果统计、反馈分布或采纳率统计。
- 未让生成器使用反馈调整建议排序、冷却或规则。
- 未写入或修改 `memories`、`profile_items`、任务、项目、目标。
- 未自动执行建议，未调用真实模型，未增加依赖。
- 未设计反馈/结果历史版本；当前表不是完整审计日志。

### 10.5 实际测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_feedback_store.py -q
# 22 passed, 4 warnings

.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_feedback_store.py tests/test_recommendations_store.py tests/test_recommendation_engine.py tests/test_recommendations_api.py tests/test_behavior_events.py -q
# 121 passed, 5 warnings（加固早期记录；最终新增过期时钟边界后又执行了全量测试）

.\.venv\Scripts\python.exe -m compileall -q app
# passed

$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest -q
# 273 passed, 1 skipped, 5 warnings in 67.26s
```

全量测试前后真实开发库 `data/personal_ai_os.db` SHA-256 一致：

```text
3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
REAL_DATABASE_UNCHANGED=1
```

另外将真实库复制到临时目录执行迁移验证：两张新表创建成功，现有 `recommendations=0`、`recommendation_runs=0`、`behavior_events=16`、`memories=18`、`profile_items=0` 保持可读，`PRAGMA foreign_key_check` 为空，真实库文件未变化。

### 10.6 B 批停止状态

1.3.4-B 已完成。当前没有 API/页面入口，普通用户还不能通过 UI 保存反馈或结果；该闭环属于下一批 1.3.4-C，需另行明确批准。

## 11. 1.3.4-C 实施记录（2026-10-12）

### 11.1 实际交付

- 在 `app/main.py` 新增反馈与结果 HTTP API，复用 B 批 `app/recommendation_feedback.py` 服务层、现有 `db()` 事务连接、Host 校验与 Same-Origin/跨站状态变更防护。
- 新增独立 API 请求模型：
  - `RecommendationFeedbackIn`；
  - `RecommendationOutcomeIn`；
  - `RecommendationMetricObservationIn`。
- API 请求模型均使用 `extra="forbid")`，客户端不能传入或覆盖 `source`、`causality_status`、记录 ID、时间戳等服务端字段；指标白名单仍由 B 批服务模型校验。
- 扩展现有「智能建议」页面脚本 `app/static/recommendations.js`，在每条建议中增加「反馈与执行结果」折叠区域；未修改建议生成规则、建议状态机、数据表结构或其他页面。
- 新增 `tests/test_recommendation_feedback_api.py`，覆盖 API、状态边界、跨站防护、输入校验、隔离、XSS 文本和前端安全接线。

### 11.2 实际 API

| 方法与路径 | 语义 | 主要响应 |
| --- | --- | --- |
| `GET /api/recommendations/{rec_id}/feedback` | 查询当前反馈 | `200 {"feedback": null}` 或当前反馈对象；建议不存在返回 404 |
| `PUT /api/recommendations/{rec_id}/feedback` | 创建或更新当前反馈 | `200 {"status":"created|updated", "feedback": {...}}` |
| `DELETE /api/recommendations/{rec_id}/feedback` | 删除当前反馈 | `200 {"deleted": true}`；反馈不存在返回 404 |
| `GET /api/recommendations/{rec_id}/outcome` | 查询当前结果 | `200 {"outcome": null}` 或当前结果对象；建议不存在返回 404 |
| `PUT /api/recommendations/{rec_id}/outcome` | 创建或更新当前结果 | `200 {"status":"created|updated", "outcome": {...}}` |
| `DELETE /api/recommendations/{rec_id}/outcome` | 删除当前结果 | `200 {"deleted": true}`；结果不存在返回 404 |

错误映射：

- 请求体不是对象、非法枚举、超长文本、非法时间、非法指标、额外字段：400。
- 建议不存在：404。
- 在非 `accepted`、已撤销采纳或已过期建议上创建/更新/删除结果：409。
- 并发唯一键竞争：沿用 B 批服务层 409。
- 跨站 PUT/DELETE 继续被中间件拒绝（403）；非 loopback Host 返回 400。

### 11.3 字段与更新语义

- 反馈请求允许字段：`vote`、`reason_codes`、`note`、`preference_scope`、`resume_after_at`。
- 结果请求允许字段：`execution_status`、`result_rating`、`actual_effect_note`、`metric_observations`、`occurred_at`。
- 未提供字段：保留 B 批当前记录中的旧值。
- 显式 `null`：作为清空/恢复默认语义处理；例如反馈备注清空为空字符串、指标清空为 `[]`。
- `planned` 结果提交显式 `occurred_at: null`，不伪造结果发生时间；终态结果未提供时间时仍由 B 批服务层写入服务端当前时间。
- 服务端始终写入 `source="user"`、`causality_status="not_inferred"`；API 不接受客户端覆盖。
- 撤销采纳后，已有结果仍可通过 GET 查看，但 PUT/DELETE 返回 409，历史记录保留且不可继续修改。

### 11.4 页面能力

- 建议卡片新增「反馈与执行结果」折叠区。
- 反馈区支持：
  - 有用/无用选择；
  - 随 vote 切换的受限原因码；
  - 备注、偏好范围和临时偏好恢复时间；
  - 保存、删除、成功/失败/409 冲突提示；
  - 保存前用确认框展示即将保存的评价、原因、偏好范围和备注。
- 结果区支持：
  - 计划执行、已完成、部分完成、未完成、不再适用；
  - 效果评级、结果说明、结果发生时间、白名单指标 JSON；
  - 仅当前 `accepted` 且未过期建议可编辑；
  - 保存前展示将保存的执行状态、评级、说明和指标数量，并再次说明“采纳不等于执行、不证明因果”。
- 空状态明确显示“尚未反馈（未知，不是负反馈）”和“尚未登记结果（缺失，不是未完成）”。
- 请求期间禁用相关按钮；409 后不重新加载列表、不清空用户输入，提示刷新后重试。
- 用户备注、结果说明、错误信息和指标文本均通过 `esc()` 放入 HTML，或使用 `textContent`；未新增 inline `onclick`。

### 11.5 必要的服务层加固

C 批审计发现结果删除如果只在 API 路由检查建议状态，直接调用服务层仍可能在撤销采纳后删除历史结果。因此在 `app/recommendation_feedback.py` 的 `delete_outcome()` 中同样复用 `_require_accepted_active_recommendation()`：

- accepted 且未过期、结果存在：允许删除；
- 非 accepted / 已撤销采纳 / 已过期：返回 409；
- accepted 且未过期但没有结果：返回 404。

该改动不改变表结构，不改变反馈删除语义，并新增服务层回归测试。

### 11.6 实际测试

```powershell
$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_feedback_api.py -q
# 27 passed, 5 warnings

.\.venv\Scripts\python.exe -m pytest `
  tests/test_recommendation_feedback_store.py `
  tests/test_recommendation_feedback_api.py `
  tests/test_recommendations_store.py `
  tests/test_recommendation_engine.py `
  tests/test_recommendations_api.py `
  tests/test_behavior_events.py -q
# 149 passed, 5 warnings（服务层删除边界补强前的相关回归）

.\.venv\Scripts\python.exe -m compileall -q app
# passed

Get-ChildItem .\app\static -Filter *.js | ForEach-Object { node --check $_.FullName }
# ALL_JS_CHECKED

$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest -q
# 300 passed, 1 skipped, 5 warnings in 49.29s
```

浏览器冒烟：

- 使用独立临时 SQLite、临时 Edge profile 和 `127.0.0.1:8146`，未使用 8000 端口。
- Edge headless + CDP 实际打开页面并执行：建议列表加载、展开反馈/结果区域、保存反馈、保存 completed 结果、采纳待处理建议、手动生成入口。
- 保存反馈和结果前确认框均被触发。
- 临时库核验结果：反馈保存为 `helpful`，结果保存为 `completed/as_expected`，`causality_status=not_inferred`，生成运行记录 1 条。
- `CONSOLE_ERRORS=[]`。

真实开发库前后一致：

```text
data/personal_ai_os.db
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
mtime: 2026-10-12 16:45:50
REAL_DATABASE_UNCHANGED=1
```

### 11.7 C 批明确未做

- 未实现 1.3.4-D 的采纳率、反馈分布、结果完成率或效果归因统计。
- 未让反馈影响推荐规则、排序、冷却或提示词。
- 未写入或修改 `memories`、`profile_items`、任务、项目或目标。
- 未自动执行建议，未调用真实大模型，未新增第三方依赖。
- 未实现反馈/结果历史版本；当前仍是一条建议对应一条可修改的当前反馈和当前结果。
- 未引入登录或多用户权限；安全前提仍是单用户、本地 loopback、单实例部署。

### 11.8 C 批停止状态

1.3.4-C 已完成 API 与页面闭环。效果统计与质量评估属于 1.3.4-D，需另行明确批准后才能开始。



## 12. 1.3.4-D 统计口径审计与默认定义（实施前锁定）

### 12.1 数据来源与时间窗口

- 建议队列统计以 `recommendations.created_at` 为时间字段。
- 生成运行统计以 `recommendation_runs.started_at` 为时间字段。
- 采纳等生命周期事件以 `behavior_events.occurred_at` 为时间字段。
- 当前反馈和当前结果通过 `recommendation_id` 关联到统计窗口内创建的建议；它们是可修改的当前记录，不伪装成历史版本流。
- 默认窗口为最近 30 天；API 可传入 `start_at`、`end_at`，统一按 UTC 解析无时区时间。
- 窗口采用半开区间 `[start_at, end_at)`；`end_at <= start_at` 返回 400。

### 12.2 指标定义

- 建议总数：窗口内创建且当前仍存在的建议数；物理删除及级联删除的反馈/结果不参与统计。
- 状态分布：按 `pending/accepted/ignored/rejected/expired` 当前物理状态统计；另列 `pending/accepted` 但已过 `expires_at` 的逻辑过期数量。
- 反馈率：当前有反馈的建议数 ÷ 窗口内建议总数；无反馈是未知，不计为 `unhelpful`。
- 有用率：`helpful` 数 ÷ 当前有反馈的建议数。
- 当前采纳率：当前物理状态为 `accepted` 的建议数 ÷ 窗口内建议总数。
- 采纳事件：单独按行为事件的 `occurred_at` 统计窗口内 `recommendation.accepted` 事件数和去重建议数；被采纳建议本身可能创建于窗口之前；事件只在功能上线后可观测，不据此反推从未采纳。
- 结果登记率：当前 `accepted` 且有当前结果的建议数 ÷ 当前 `accepted` 建议数。
- 完成率：`execution_status=completed` 的当前结果数 ÷ 全部当前结果数；没有结果代表缺失，不代表未完成。
- 效果评级分布：`much_better/better/as_expected/worse/much_worse/unclear` 分别统计；`unclear` 是不明确，不是负面效果。
- 指标差异：仅聚合同一 `metric + unit` 且同时存在 `expected_value`、`observed_value` 的用户主动登记观察；只给数值差异，不证明建议导致变化。
- 质量评估只返回证据覆盖率、反馈覆盖率、结果登记覆盖率、可比指标样本数等独立指标；第一版不合成综合质量分。
- 分母为 0 时比例返回 `null`，不展示为 0%；分母小于 10 的比例标记为 `insufficient_data`，但仍返回分子、分母和原始比例。

### 12.3 明确限制

- 不回填历史数据，不推断历史采纳、历史反馈修改或历史结果修订。
- 统计接口只读，不触发建议生成或模型调用，不写入行为事件、记忆、画像或任务数据。
- 不返回建议标题正文、反馈备注、结果说明等非必要自由文本。
- 相关性、用户主观评级和指标数值差异均不得表述为因果结论。


## 13. 1.3.4-D 实施回填（2026-10-12）

### 13.1 实际修改范围

新增：

- `app/recommendation_stats.py`：只读建议效果统计服务。
- `tests/test_recommendation_stats.py`：统计服务、API、只读性、隐私、时间边界和前端静态回归测试。

修改：

- `app/main.py`：新增只读路由 `GET /api/recommendation-statistics`，复用现有 Host 防护与 SQLite 连接，不新增表、不写数据。
- `app/static/index.html`：在「智能建议」页新增只读「建议效果统计」面板；未修改其他页面。
- `app/static/recommendations.js`：新增统计加载、错误态、空/零分母展示、状态/反馈/结果/评级/运行分布与限制说明渲染。
- `docs/plans/phase-4-1.3.4-recommendation-feedback.md` 与 `docs/plans/phase-4-personalization-engine.md`：记录本批口径、实现与验证结果。

### 13.2 实际 API

`GET /api/recommendation-statistics`

查询参数：

- `start_at`：可选，ISO-8601；无时区按 UTC。
- `end_at`：可选，ISO-8601；无时区按 UTC；默认服务端当前时间。
- 默认窗口：最近 30 天；半开区间 `[start_at, end_at)`。
- `end_at <= start_at` 或时间格式非法：返回 400。

响应只包含聚合数据，不返回建议标题/正文、反馈备注、结果说明、完整提示词、密钥、运行错误自由文本等非必要个人文本。主要结构：

- `definition_version`：`recommendation_feedback_stats_v1`
- `window`
- `recommendations`：总数、当前状态、类别、覆盖状态、逻辑过期 active 数、证据存在率
- `runs`：运行状态分布、正常结束数、失败数、模型启用运行数
- `feedback`：当前反馈数、vote 分布、反馈率、有用率、原因码与偏好范围分布
- `adoption`：当前采纳数/率，以及按行为事件 `occurred_at` 统计的采纳事件与去重建议数
- `outcomes`：当前结果数、当前 accepted 建议结果登记率、保留历史结果数、执行状态分布、完成率、可比指标组
- `ratings`：效果评级数量、占比和分母，`unclear` 独立列示
- `quality`：证据覆盖、反馈覆盖、结果登记覆盖、可比观察数；`composite_score=null`
- `limitations`：当前记录/历史覆盖/缺失语义/非因果限制

### 13.3 已实现的关键口径

- 建议队列：按 `recommendations.created_at` 纳入半开时间窗口。
- 生成运行：按 `recommendation_runs.started_at` 统计；`success`、`no_evidence`、`degraded` 均为正常结束但保留独立状态，不把失败运行算作成功。
- 当前反馈与结果：JOIN 到窗口内创建的建议，统计可修改的当前值；不声称还原历史版本。
- 无反馈：未知，不计入 `unhelpful`。
- 有用率：`helpful / 当前有反馈建议数`。
- 当前采纳率：窗口建议中当前物理状态为 `accepted` 的比例。
- 生命周期事件：按 `behavior_events.occurred_at` 统计窗口内 accepted/ignored/rejected/reopened 事件；若建议创建于窗口之前但事件发生在窗口内，事件仍计入，且页面明确标注历史覆盖不完整。
- 结果登记率：当前 `accepted` 建议中有当前结果的比例；无结果代表未观测，不代表未执行。
- 完成率：`execution_status=completed` 的当前结果数 / 全部当前结果数。
- 效果评级：六类评级分别给出数量、分子、分母、四舍五入到四位小数的比例和样本状态；`unclear` 不计入负面。
- 指标差异：只聚合同一 `metric + unit` 且同时有 expected/observed 数值的用户主动登记观察；输出均值差和 `not_inferred`，不证明建议导致变化。
- 零分母：比例字段返回 `null`，页面显示“暂不可计算（分母为 0）”，不显示虚假 0%。
- 样本量：分母小于 10 的比例标记 `insufficient_data`，但仍返回分子、分母和原始比例。

### 13.4 安全与边界

- 统计服务与 API 均为只读；测试通过表行数前后比对确认统计请求不写入任何业务表。
- 未触发模型调用，未修改建议生成规则、排序、冷却、提示词或状态机。
- 未写入 `memories`、`profile_items`、任务、项目、目标，也未自动执行建议。
- SQL 均使用参数绑定；没有新增第三方依赖。
- 前端新增统计文本通过既有 `esc()` 渲染；未新增 inline 事件处理器。
- 统计面板位于「智能建议」页；静态测试确认不会出现在「项目与任务」页。
- 当前系统仍是无登录、本地 loopback、单用户部署模型；本批未引入身份认证或多用户隔离。

### 13.5 实际验证

```powershell
$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_stats.py -q
# 10 passed, 5 warnings

.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_stats.py tests/test_recommendation_feedback_api.py -q
# 37 passed, 5 warnings

.\.venv\Scripts\python.exe -m compileall -q app
# PYTHON_COMPILE_OK

Get-ChildItem .\app\static -Filter *.js | ForEach-Object { node --check $_.FullName }
# ALL_JS_CHECKED

.\.venv\Scripts\python.exe -m pytest -q
# 310 passed, 1 skipped, 5 warnings in 89.36s
```

浏览器冒烟（独立临时 SQLite、临时 Edge profile、端口 8147/9257，未使用 8000 端口）：

- 智能建议页统计面板加载成功，且不存在于项目页。
- 建议列表 2 条，证据、反馈与执行结果折叠区可见。
- 页面展示反馈率、有用率、当前采纳、结果登记率、完成率、评级数量/占比、生成运行分布。
- 零分母/缺失指标显示“暂不可计算”或“暂无可比指标观察”，未伪装成 0%。
- 页面明确显示当前记录、历史覆盖不完整和“不证明建议导致变化”。
- 聚合 API 未返回测试标记的建议标题、反馈备注或结果说明自由文本。
- `CONSOLE_ERRORS=[]`。

真实开发库核验：

```text
data/personal_ai_os.db
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
Length: 258048
mtime: 2026-10-12 16:45:50
```

测试前后哈希一致，真实开发库未被 D 批测试或浏览器冒烟修改。

### 13.6 已知限制与未做事项

- 当前反馈/结果仍是一条建议一条可修改当前记录，不包含完整历史版本。
- 生命周期事件仅覆盖事件表上线后的可观测数据，不能还原更早历史。
- 本批不做反馈驱动的推荐排序/策略调整，不做综合质量分。
- 本批不做 1.3.4-E，也不进入 1.3.5。
- 当前目录不是 Git 仓库（`git rev-parse` 返回 `fatal: not a git repository`），因此无法使用 Git diff 审计；已通过文件清单、测试隔离和真实库哈希进行核验。

## 14. 1.3.4-E 安全与质量最终验收（2026-10-12）

### 14.1 审计范围

本批先审计、后最小修复，未新增业务功能。实际复核：

- `app/main.py`：Host loopback 校验、同源状态变更防护、反馈/结果/统计 API、请求体与状态码映射。
- `app/recommendations.py`：建议状态机、删除、去重、运行记录与错误脱敏。
- `app/recommendation_feedback.py`：反馈/结果当前记录、唯一约束、外键、同事务事件、撤销采纳后的结果边界。
- `app/recommendation_stats.py`：半开时间窗口、统计口径、只读事务、聚合白名单、参数绑定与分页/变量上限。
- `app/behavior_events.py`：生命周期事件只作 append-only 事件，不回填历史。
- `app/static/recommendations.js` 与 `app/static/index.html`：纯文本渲染、409 保留输入、防重复提交、只读统计面板与非内联事件委托。
- 反馈/结果/统计/建议存储/建议 API/生成引擎专项测试与全量测试。

部署前提维持不变：**本地 loopback、单实例、单用户、无登录**。Host/Origin 防护是本机纵深防护，不等同身份认证，不表示可公网部署。

### 14.2 本批最小修复

| 修复点 | 文件 | 说明 |
| --- | --- | --- |
| 亚秒统计边界 | `app/recommendation_stats.py` | SQL 先扩大到存储秒，再由 Python 精确执行 `[start_at, end_at)`；修复结束时间为 `xx:xx:00.500` 时错误纳入 `xx:xx:00` 存储记录的问题。响应声明 `time_resolution=stored_seconds`。 |
| 同响应读快照 | `app/recommendation_stats.py` | 同一次统计响应复用一个 SQLite deferred 读事务，服务自身创建时最终 `ROLLBACK` 释放；若调用方已有事务则复用且不代替调用方回滚。 |
| 聚合白名单 | `app/recommendation_stats.py` | 原因码只统计服务层允许值；指标观察只统计白名单 `metric/unit` 与有限数字，拒绝损坏值、`NaN/Inf`，不同单位不合并。 |
| 大窗口变量上限 | `app/recommendation_stats.py` | 建议 ID 按 500 个分块查询，避免大窗口超过 SQLite 变量数量限制；指标分组复用已读取 outcome 行，避免重复扫描。 |
| 反馈写入竞争 | `app/recommendation_feedback.py` | 插入/更新使用带 `EXISTS recommendations` 的条件 SQL 和 `rowcount` 检查，不再只依赖先查后写。 |
| 结果写入竞争 | `app/recommendation_feedback.py` | 插入/更新使用“建议仍为 accepted 且未过期”的条件 SQL；写入后、记录行为事件前在同一事务再次复核，避免竞争状态变化后产生孤立 outcome 事件。 |
| 反馈删除竞争 | `app/recommendation_feedback.py` | 删除使用父建议仍存在的条件 SQL；删除后、记录删除事件前再次复核父建议。 |
| 结果删除竞争 | `app/recommendation_feedback.py` | 删除使用 accepted/未过期条件 SQL；删除后、记录删除事件前再次复核，撤销采纳后历史结果仍保留但不可删除。 |
| 畸形请求体 | `tests/test_recommendation_feedback_api.py` | 新增回归验证既有 API 对非对象/畸形 JSON 返回 400/422，不返回 500、traceback 或 SQLite 内部信息；本批未改路由实现。 |

未修改表结构，未新增依赖，未修改生成规则、排序、冷却、提示词、前端视觉结构或真实用户数据。

### 14.3 新增/扩展回归测试

- `tests/test_recommendation_stats.py`：从 D 批 10 项扩展到 14 项，新增亚秒边界、损坏词表/不可比指标过滤、调用方事务复用、1002 个 ID 的分块查询。
- `tests/test_recommendation_feedback_store.py`：从 B/C 批 24 项扩展到 26 项，新增结果插入/更新竞争下无孤立事件、旧结果保留；新增反馈删除与结果删除竞争保护。
- `tests/test_recommendation_feedback_api.py`：新增畸形 JSON 不触发 500，不泄露 traceback/sqlite 内部信息。

新增测试均使用独立临时 SQLite，同时设置 `DATABASE_PATH` 与 `app.main.DB_PATH`，未使用真实开发库构造数据。

### 14.4 实际命令与结果

```powershell
$env:LLM_API_KEY=''
$env:RECOMMENDATIONS_USE_MODEL=''

.\.venv\Scripts\python.exe -m pytest tests/test_recommendation_feedback_store.py -q
# 26 passed, 4 warnings

.\.venv\Scripts\python.exe -m pytest `
  tests/test_recommendation_feedback_store.py `
  tests/test_recommendation_feedback_api.py `
  tests/test_recommendation_stats.py `
  tests/test_recommendations_store.py `
  tests/test_recommendations_api.py `
  tests/test_recommendation_engine.py -q
# 158 passed, 5 warnings in 53.69s

.\.venv\Scripts\python.exe -m pytest -q
# 319 passed, 1 skipped, 5 warnings in 91.66s

.\.venv\Scripts\python.exe -m compileall -q app
# compileall: PASS

node --check app\static\analytics.js
node --check app\static\connectors.js
node --check app\static\doc-upload.js
node --check app\static\memory-candidates.js
node --check app\static\persona.js
node --check app\static\recommendations.js
node --check app\static\work.js
# all 7 JavaScript files: PASS
```

5 个 warnings 为既有 FastAPI `on_event` / Starlette TestClient 弃用提示；本批不为消除警告做无关升级。

### 14.5 临时数据库浏览器冒烟

使用系统自带 Microsoft Edge headless + CDP，未安装 Playwright 或新依赖；使用临时 SQLite、临时 Edge profile 和非 8000 端口。

- 首次 CDP 冒烟：服务与页面加载成功，浏览器实际保存 `planned` outcome（服务日志记录 `PUT /api/recommendations/1/outcome 200`）；测试脚本随后因等待旧按钮重新渲染而卡住，属于冒烟脚本问题，已终止。
- 复跑/续验：端口 `8148/9258`，页面加载、进入「智能建议」、展示统计面板、证据/反馈/结果折叠区、XSS 转义、既有 `planned` 结果、撤销采纳后结果只读保留、聚合响应不含秘密标记均通过。
- 最终输出：`SMOKE PASS`。
- 复核结果：`8000` 端口未被本批测试或冒烟占用；临时 Uvicorn/Edge 进程已终止，仅剩端口 `TIME_WAIT` 状态，无监听进程。

动态验证覆盖：

- 恶意 `<img src=x onerror=...>` 作为标题/备注/正文时只显示转义文本，未创建注入图片，未设置 `window.__SMOKE_XSS`。
- 浏览器保存计划结果成功；撤销采纳后结果仍显示“计划执行”，保存/删除按钮禁用。
- 统计页显示窗口、分子/分母、样本不足、当前记录/历史覆盖不完整与“不证明建议导致变化”提示。
- 聚合统计响应不包含测试秘密文本或结果说明自由文本，`composite_score=null`。

### 14.6 真实开发库隔离证据

测试、编译、JS 检查和浏览器冒烟后复核：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

哈希、长度、修改时间与本批开始前基线一致，真实开发库未被 E 批测试或冒烟修改。

### 14.7 已检查但未发现问题的事项

- GET 接口保持只读；统计请求不会触发生成、模型调用或业务表写入。
- 客户端不能传入/覆盖 `source`、`causality_status`、记录 ID、创建时间或更新时间。
- 无反馈仍是未知，不计为 `unhelpful`；无结果仍是未观测，不计为未完成。
- `unclear` 独立统计，不归入负面评级。
- 当前反馈/结果统计与 append-only 生命周期事件分开呈现，不声称还原完整历史。
- 一对一反馈/结果约束由数据库唯一约束兜底；级联删除依赖并实际验证 `PRAGMA foreign_keys=ON`。
- 所有新增统计 SQL 使用参数绑定；分页仍有上限。
- 建议、反馈、结果、统计链路未写入 `memories` 或 `profile_items`，未修改任务、项目、目标。
- 真实模型开关默认关闭；本批没有真实模型网络请求。

### 14.8 已确认的遗留限制（不阻塞本地单用户阶段收口）

1. **无登录/无多用户隔离**：当前只适合本机可信单用户 loopback；未来公网或远程访问必须先做身份认证、授权与多用户数据隔离。
2. **单实例协调限制**：进程内锁不覆盖多进程；数据库唯一约束可阻止重复活跃建议，但不保证所有运行记录/生成流程跨进程完全串行。
3. **历史版本缺失**：反馈和结果仍是可修改当前记录，不是完整审计历史；生命周期事件也无法补全事件表上线前的历史。
4. **真实模型未端到端验收**：本批验证默认关闭和 mock/异常路径，不验证真实供应商的费用、超时、质量或隐私合同。
5. **旧库唯一索引兼容限制**：历史重复数据迁移场景可能跳过部分唯一索引，需要未来只读诊断和用户确认修复流程。
6. **当前目录不是 Git 仓库**：`git rev-parse` 返回 `fatal: not a repository`，无法声称审阅过 Git diff；本批改用文件清单、专项/全量测试、临时库冒烟和真实库哈希核验。

### 14.9 E 批结论

1.3.4-E 范围内发现的可修复正确性、并发与隐私问题已完成最小修复并有回归测试。全量测试 `319 passed / 1 skipped`，编译、全部相关 JS 语法检查、临时数据库浏览器冒烟和真实库隔离核验通过。

在上述本地 loopback、单实例、单用户、无登录部署前提下，1.3.4「建议反馈与结果追踪」代码与测试层面具备阶段收口条件；是否正式进入 1.3.5 等待人工批准。

