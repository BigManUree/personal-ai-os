# Personal AI OS 1.3.3：个性化建议引擎 — 现状审计与实施计划

> 状态：**E 批（安全与质量独立验收）已完成并通过测试；1.3.3 A→E 收口，未开始 1.3.4**。
> 日期：2026-10-12。本轮只审计与写计划，**未修改任何业务代码、数据库、API 或前端**，未调用大模型。
> 基线已在本轮重新复跑：A 批基线 `pytest -q` = **161 passed, 1 skipped, 5 warnings**（2026-10-12，项目 venv）。
> 上游文档：`phase-4-personalization-engine.md`、`phase-4-1.3.2-behavior-analytics.md`。

---

## 一、实际审计的文件与模块

| 模块 | 文件 | 审计目的 |
| --- | --- | --- |
| API 与 DB 初始化 | `app/main.py`（2358 行） | 全部路由、建表 DDL、幂等迁移、模型调用、聊天链路 |
| 行为事件 | `app/behavior_events.py` | 事件类型、元数据校验、密钥拦截、去重键 |
| 确定性统计 | `app/analytics.py`（501 行） | `overview()` / `compare_periods()` 真实返回结构 |
| 画像提议 | `app/profile_propose.py` | 最小上下文、JSON 解析、Pydantic 校验、密钥过滤、模型降级 |
| 记忆提取 | `app/memory_extract.py` | 结构化 JSON 模型调用的既有范式 |
| 检索层 | `app/retriever.py` | 关键词检索、未来向量检索替换点 |
| 连接器 | `app/connectors.py`、`connector_scheduler.py`、`github_connector.py`、`httpapi_connector.py`、`secret_crypto.py` | 数据源/同步日志/凭据存储现状 |
| 前端 | `app/static/index.html`、`analytics.js`、`persona.js`、`work.js` 等 | 单页导航、`esc()` 转义、各页加载约定 |
| 测试 | `tests/` 共 14 个文件 | fixture 约定（`isolated_db` + monkeypatch + mock LLM） |
| 文档 | `README.md`、`docs/plans/phase-4-personalization-engine.md`、1.3.2 计划 | 启动/配置/API 文档约定 |

---

## 二、现有功能及代码证据

### 2.1 1.3.1 个人画像引擎 —— **已完成，可作为 1.3.3 的事实来源**

- 表：`profile_items`（category / title / content / meta / status / is_user_stated / confidence / source_type / source_id / source_detail / valid_from / valid_until / 三个时间戳）+ `profile_item_revisions`。
- 类别七类：`basic, skill, interest, goal, preference, observation, inference`；状态四态：`pending, confirmed, expired, rejected`。
- 红线已落地：模型提议恒为 `status='pending', is_user_stated=0`，`inference` 置信度被 clamp 到 ≤0.5；用户确认后才是事实。
- API（`app/main.py` 1744–1960 行附近）：list / create / patch / confirm / reject / expire / delete / history / `POST /api/profile-items/propose`，共 9 个路由。
- `app/profile_propose.py`：只发送【档案摘要 + 已确认记忆（≤20 条，每条截断 400 字）+ 进行中目标（≤15 条）】；`response_format=json_object`、temperature=0、45s 超时、2 次重试；`parse_proposals()` 逐条 Pydantic 校验、去重、`looks_like_secret()` 密钥过滤；无 `LLM_API_KEY` 时 propose 路由返回 400，CRUD 不受影响。

### 2.2 1.3.2 行为与习惯统计 —— **已完成，可作为建议的确定性证据源**

- 表 `behavior_events`：**只存事件元数据**（类型、时间、主体 id、项目/目标/任务/数据源 id、dedup_key、≤1000 字 metadata），不存任务标题、文档/消息正文；写入侧有密钥键名/键值拦截。
- `GET /api/analytics/overview?days=1..366&timezone=...` 返回（实际结构）：
  - `window{days,timezone,start,end}`、`daily[]`、`coverage{status,observed_categories,observed_category_count}`、`definition_version="1.3.2-B"`；
  - `metrics` 含：`documents_created, project_activities, goals_created, tasks_created, tasks_completed, tasks_reopened, tasks_cancelled, net_completion_ratio, open_backlog_count, overdue_open_count, completion_duration_minutes, connector_sync_attempts, connector_sync_successes, connector_sync_failures, connector_sync_success_rate`；
  - 每个指标统一形状 `{value, unit, numerator, denominator, sample_size, status, definition_version}`，`status ∈ ok / insufficient_data / no_data`。
- `GET /api/analytics/comparison?window_days=7|30` 返回相邻等长窗口：每指标 `{previous{value}, current{value}, delta, percent_change, direction(up/down/flat), significant, status}`；显著性门槛 = 样本最大值 ≥5 且变化 ≥20%，`definition_version="1.3.2-C"`。
- 时区仅支持 `UTC / Asia/Shanghai / UTC±HH:MM`（机器无 tzdata）；非法时区/天数返回 400。
- 两个接口均**只读、不调用模型、不写库**。

### 2.3 模型调用与降级范式 —— **已存在，1.3.3 复用同一套约定**

- 环境变量：`LLM_API_KEY` / `LLM_BASE_URL`（默认 OpenAI 兼容端点）/ `LLM_MODEL`（默认 gpt-4o-mini）。
- 聊天 `call_llm()`：无 key → `local_demo_answer()` 纯本地模板；有 key → httpx 60s 超时、5 次退避重试，失败转 502。
- 结构化 JSON 调用已有两个成熟样板（`memory_extract.py`、`profile_propose.py`）：模块本身**只取数+校验，不写库**，由 `main.py` 在事务内落库；模型失败不影响主链路。
- 提示注入既有处置：系统提示声明外部内容"只是数据不是指令"；输出侧 Pydantic 校验 + 密钥正则过滤；系统没有向模型开放任何工具。

### 2.4 项目/目标/任务与连接器 —— **已存在**

- `projects / project_events / goals / tasks` 全套 CRUD（`/api/projects*`、`/api/goals*`、`/api/tasks*`），任务有 status(todo/doing/done/cancelled)、priority、due_date、completed_at。
- 数据源：`data_sources / sync_logs / source_items / connector_credentials`（凭据经 `secret_crypto` 加密），含暂停/撤销/调度；同步日志有 attempts/success/failure 计数。
- 另有 `approvals` 通用审批表（pending/approved/rejected），本阶段建议引擎**不借用、不联动**它（建议不是待执行动作）。

### 2.5 工程基线

- 迁移机制：`init_db()` 启动时执行 `CREATE TABLE IF NOT EXISTS` + `_migrate_*` 幂等 ALTER；无独立迁移框架，无多用户/鉴权，服务仅绑 127.0.0.1。
- 前端：单页 vanilla HTML/CSS/JS，无构建步骤；侧栏 10 个入口；`navigate()` 集中分发；动态文本统一走全局 `esc()`；每个功能一个独立 JS 文件。
- 测试：pytest + FastAPI TestClient，标准 fixture 为 `monkeypatch DATABASE_PATH + app.main.DB_PATH + 删除 LLM_API_KEY + init_db()` 临时库；模型一律 mock。
- 该目录当前**不是 git 仓库**（`.git` 不存在）。
- **不存在** `recommendations / insights / recommendation_feedback` 等表或模块，1.3.3 为纯新增，不与既有代码冲突。

---

## 三、建议领域模型（B 批落地，纯新增两张表）

### 3.1 `recommendations`（建议记录）

| 字段 | 类型/约束 | 用途与规则 |
| --- | --- | --- |
| id | INTEGER PK AUTOINCREMENT | 稳定 ID |
| title | TEXT NOT NULL，≤200 字 | 建议标题 |
| content | TEXT NOT NULL，1–2000 字 | 建议正文（具体可执行的一步或几步） |
| category | TEXT NOT NULL，CHECK ∈ `task, project, goal, learning, habit` | 任务/项目/目标/学习/习惯（首版**不设 system 类**，连接器健康不作为个人建议） |
| priority | TEXT NOT NULL DEFAULT 'normal'，CHECK ∈ `low, normal, high` | 程序规则决定，模型不可改 |
| status | TEXT NOT NULL DEFAULT 'pending'，CHECK ∈ `pending, accepted, ignored, rejected, expired` | 见 3.3 状态机 |
| evidence_json | TEXT NOT NULL DEFAULT '[]' | 证据数组，见 3.2，整体 ≤3000 字 |
| window_json | TEXT NOT NULL DEFAULT '{}' | 证据时间窗口 `{days,timezone,start,end}` 与统计定义版本 |
| coverage_status | TEXT NOT NULL DEFAULT 'no_data' | 生成时证据覆盖率快照（ok/partial_data/insufficient_data/no_data） |
| expected_benefit | TEXT NOT NULL DEFAULT ''，≤500 字 | 预期收益（措辞为"可能/有助于"，不承诺结果） |
| risk_level | TEXT NOT NULL DEFAULT 'low'，CHECK ∈ `low, medium, high` | 程序规则决定 |
| risk_note | TEXT NOT NULL DEFAULT ''，≤500 字 | 风险与不适用情形 |
| confidence | REAL NOT NULL，0–1 | 见 3.4 置信度语义；程序档位，模型不可提升 |
| source | TEXT NOT NULL，CHECK ∈ `rule, hybrid` | rule=纯规则模板；hybrid=规则起草+模型改写文案 |
| rule_code | TEXT NOT NULL DEFAULT ''，≤80 字 | 产生该建议的确定性规则代码（如 `task.overdue_open`），可追溯/可去重 |
| generation_version | TEXT NOT NULL | 规则+提示词版本号，首版 `1.3.3-C@1` |
| dedup_key | TEXT NOT NULL（普通索引，**非 UNIQUE**，见 3.5 与 B 批回填） | 见 3.5 |
| expires_at | TEXT NOT NULL | 有效期（UTC ISO）；过期后惰性标记，不再展示为待处理 |
| created_at / updated_at / reviewed_at | TEXT NOT NULL / NOT NULL / 可空 | UTC ISO |

索引：`(status, priority, id)`、`(category, status)`、`(expires_at)`。

建议与个人事实**物理分开**：本表任何行都不会自动写入 `memories` 或 `profile_items`；采纳建议也不改变画像（这是 1.3.4 以后经专门设计才可能发生的事）。

### 3.2 证据对象（evidence_json 数组元素）

```json
{
  "kind": "metric|task|project|goal|profile_item",
  "ref_type": "analytics_metric|tasks|projects|goals|profile_items",
  "ref_id": 12,
  "label": "当前超期开放任务",
  "value": 3,
  "detail": "窗口 2026-10-05~10-12，样本 3，状态 insufficient_data",
  "definition_version": "1.3.2-B"
}
```

- 证据只引用**稳定 ID + 指标快照**，不复制文档/消息正文；任务证据只存 id 与计数，前端在本地用 id 拉取标题展示，不把标题发给模型。
- 每条建议至少 1 条证据；无证据不得落库。

### 3.3 状态机

```
pending ──accept──▶ accepted ──reopen──▶ pending   （本阶段"采纳"不执行任何动作，故允许撤销）
pending ──ignore──▶ ignored                       （知道了，保留记录，不再提醒）
pending ──reject──▶ rejected                      （明确不喜欢这类建议，进入冷却）
pending/accepted ──过期──▶ expired                （惰性：查询/生成时把 expires_at < now 的非终态行批量置 expired）
```

- ignored/rejected/expired 为终态，不可再流转（如需恢复，用户删除后下次重新生成）。
- 非法转移返回 409；未知 id 返回 404；重复提交同一转移返回 409（天然防重复）。
- DELETE 为用户主动物理删除（与 ignore 的"保留但消失"区分）。

### 3.4 置信度语义（必须在 UI 与 README 写明）

`confidence` 表示**程序对"该建议适用于用户当前状况"的把握**，由样本量、覆盖率、规则强度分档（如 0.5 / 0.7 / 0.85 三档），**不是成功概率，不是人格判断**；模型只能改写文案，不能修改 confidence、priority、risk_level、证据。

### 3.5 去重、过期与冷却

- `dedup_key = sha256(rule_code | category | 证据指纹 | 窗口归一化)`，证据指纹如任务 id 有序列表、指标名+比较周期。
- 已存在 `pending/accepted` 且未过期的同 key → 跳过；已过期同 key → 允许生成新行。
- `rejected` 同 key 进入 **30 天冷却**（常量 `REJECT_COOLDOWN_DAYS=30`），手动/自动触发生成都跳过并在 run 中计数为 `suppressed_rejected`，避免反复打扰。

### 3.6 `recommendation_runs`（生成运行记录，失败可查）

| 字段 | 用途 |
| --- | --- |
| id, triggered_by(`manual`，首版只有手动), use_model(0/1) | 触发方式 |
| window_days, timezone, window_start, window_end | 输入窗口 |
| status | `success / no_evidence / degraded / failed` |
| candidates, created, duplicates, suppressed_rejected, expired_marked | 计数 |
| error | 可安全展示的错误摘要（**仅异常类型与短消息，脱敏后**，禁止记录 key/令牌/请求头） |
| started_at, finished_at | 耗时排查 |

日志纪律沿用 `profile_propose` / 连接器：不打印证据正文与任何凭据。

---

## 四、生成流程（C 批落地，模块 `app/recommendations.py`）

```
手动触发 POST /generate
  └─ collect_evidence(conn, days, timezone, now)
       ├─ analytics.overview() + analytics.compare_periods()   # 数字，程序算
       ├─ confirmed profile_items（仅 status=confirmed；pending/inference 不用）
       ├─ active goals / open+overdue tasks / 活跃项目及最近活动时间
       └─ coverage 汇总；全程只读
  └─ 确定性规则集 evaluate_rules(evidence)
       每条规则：硬阈值 + 样本门槛(复用 MIN_STRONG_SAMPLE=5) + 过期/无数据判断
       → 0..N 个 Draft{rule_code, category, priority, risk, confidence 档位,
                      evidence 引用, 模板 title/content, expected_benefit}
  └─ 候选为 0 且 coverage=no_data/insufficient → run=no_evidence，直接返回，不建建议
  └─ 去重/冷却过滤（dedup_key、rejected 30 天）
  └─ 可选模型步骤 explain_with_model(drafts, minimal_stats)
       发送内容：指标数字、窗口、已确认画像的类别+content(截断)；
                 不发送任务标题、文档/消息正文、记忆全文
       模型职责：仅按 draft 顺序改写 title/content 措辞，输出 JSON 数组（带 rule_code 对齐）
       非法输出/超时/限流/密钥模式 → 该批降级为规则模板，run=degraded
       无 LLM_API_KEY 或 use_model=false → 直接 rule 模板，run=success
  └─ Pydantic 总校验 + looks_like_secret 过滤 + 长度夹紧
  └─ 单事务写入 recommendations + recommendation_runs（失败整体回滚）
```

### 4.1 首版确定性规则候选（C 批按实现成本排序，至少前 3 条）

| rule_code | 触发条件（程序判断，全部要求样本/数据真实存在） | category |
| --- | --- | --- |
| `task.overdue_open` | `overdue_open_count > 0`（当前快照，附任务 id 列表） | task |
| `task.completion_drop` | 对比窗口 `tasks_completed.significant=true` 且 direction=down | habit/task |
| `project.inactive` | 某 active 项目在窗口内 0 条 project 活动事件且项目创建早于窗口起点（只提示"可安排一次检查"，**不得断言放弃/懒惰**） | project |
| `goal.without_progress` | active 目标窗口内无关联任务/事件，且目标创建早于窗口起点 | goal |
| `backlog.growing` | `tasks_created` 显著高于 `tasks_completed` 且 `open_backlog_count` 达阈值 | task |

规则文案必须包含观测窗口与样本口径；`no_data` 解释为"系统没有观测到记录"，绝不写成"你什么都没做"。

### 4.2 提示注入与隐私边界

- 进模型的画像/任务等字段一律包裹为"不可信数据"；模型无任何工具，注入文本最坏只可能成为待审核建议正文，且经长度/密钥/枚举校验。
- 测试中显式断言：含"忽略以上指令"的画像内容不会改变规则数量与结构化字段；请求体不含任务标题与文档片段。
- 不自动定时生成、不自动重试外部调用、不向连接器/聊天/记忆/画像回写。

---

## 五、API 与页面计划（D 批）

### 5.1 API（沿用现有命名风格，全部挂 `/api/recommendations`）

| 方法/路径 | 行为 |
| --- | --- |
| GET `/api/recommendations?status=&category=&limit=` | 列表（默认隐藏 expired/rejected，可显式查），惰性过期在此时处理 |
| GET `/api/recommendations/{id}` | 详情含证据、窗口、风险 |
| POST `/api/recommendations/generate` | body `{days?:7\|14\|30, use_model?:true, categories?:[]}`；同步生成，返回 run 摘要与新建列表；无证据返回 200 + `created=0,status=no_evidence` |
| POST `/api/recommendations/{id}/accept` `/ignore` `/reject` `/reopen` | 状态流转；非法转移 409 |
| DELETE `/api/recommendations/{id}` | 物理删除 |
| GET `/api/recommendations/runs?limit=` | 生成历史与脱敏失败原因 |

参数非法沿用现有约定：枚举/范围错误 400，Pydantic 类型错误 422，模型端点失败 502（生成接口降级时不报错，靠 run.status=degraded 表达）。

### 5.2 前端（新增 `app/static/recommendations.js` + 侧栏「智能建议」）

- 顶部：生成区（窗口选择 7/14/30 天、模型开关默认随是否有 key、生成按钮带防重复 disabled）、最近一次 run 状态/失败原因/降级提示。
- 列表卡片：优先级色条、类别、标题正文、证据 chip（指标名+值+窗口口径）、预期收益、风险等级、置信度（带"含义"小注）、有效期、状态按钮（采纳/忽略/拒绝），accepted 可"撤销"，可删除；`<details>` 展开证据与运行版本。
- 三态齐全：首次空状态引导去生成；`no_evidence` 明确"数据不足，不是你没做事"；加载/错误提示；所有渲染走 `esc()`。
- 不新增依赖、不改构建方式。

---

## 六、A→E 分批、验收与禁止事项

| 批次 | 范围（文件） | 验收标准 | 禁止事项 |
| --- | --- | --- | --- |
| **A（本轮，已完成）** | 仅本计划文档 + 总计划状态指针 | 审计结论有代码行级证据；基线复跑 161 passed | 不改业务代码/DB/前端，不调模型 |
| **B：领域模型与存储** | `main.py` init_db 新增两表；新建 `app/recommendations.py`（Pydantic 模型、create/list/get/transitions/dedup/过期/run 记录的服务层）；`tests/test_recommendations_store.py` | 服务层单测：CRUD、状态机非法转移、404/409、去重、rejected 冷却、过期惰性标记、run 计数；旧库连续两次 init_db 无损、旧数据计数不变 | 不暴露 HTTP 路由；不做规则引擎；不调模型 |
| **C：证据驱动生成引擎** | `app/recommendations.py` 增 collect_evidence / evaluate_rules / explain_with_model / generate；`tests/test_recommendations_engine.py` | 规则在构造数据下命中/不命中；样本不足→no_evidence；同输入同输出（确定性）；mock 模型：成功对齐、非法 JSON/超时/500→degraded 降级为模板；无 key→rule-only；请求体不含任务标题/文档内容；注入文本无副作用；数字全部来自 analytics 快照 | 不挂 HTTP 路由；不做前端；不自动写记忆/画像 |
| **D：API 与管理页面** | `main.py` 新路由；`static/recommendations.js`、`index.html` 导航；`tests/test_recommendations_api.py` | 接口全套 happy/非法路径；generate 串行去重（连发两次第二次 created=0）；页面挂载回归（HTML 含入口/script，仿 analytics 测试）；Edge headless 临时库冒烟 | 不实现 1.3.4 反馈字段/效果追踪；不做定时自动生成 |
| **E：安全与验收** | README、本计划回填、必要 `docs/errors/` | 专项注入/密钥/降级测试齐；全量 pytest 绿；compileall + node --check；临时库冒烟；输出本阶段验收报告 | 不开始 1.3.4 |

B–E 每批完成后跑测试、汇报、停止，等待明确批准再进入下一批。

### 明确不属于 1.3.3（留给后续版本）

- 建议反馈评分、实际执行结果、预期 vs 实际对比、反馈驱动的画像偏好更新（**1.3.4**）。
- 仪表盘聚合（**1.3.5**）；定时自动生成建议（最早 1.5 主动提醒再评估）。
- 建议驱动的自动执行、外部操作、Agent 工具调用（**1.4**）。
- 多用户/鉴权体系、向量检索、PostgreSQL 迁移。

---

## 七、未验证事项与风险

1. **数据量风险**：`behavior_events` 自 1.3.2 才开始累积（另有一次性手动 backfill），真实窗口内样本可能长期为 `insufficient_data`。这是数据现实，不通过放宽门槛掩盖；首月建议主要由任务快照类规则（overdue/backlog）支撑。
2. **项目停滞判定**需直接查项目最近活动时间（overview 不返回该值），C 批将以 `project_events`/`behavior_events` 两路取最近时间，规则中写明口径。
3. **模型成本与稳定性**：生成仅手动触发；单次请求草稿数设上限（≤10），上下文严格截断。
4. **置信度误导风险**：通过固定档位 + UI 文案 + README 定义缓解，不允许模型自由打分。
5. **过期建议的清理**：首版只做惰性标记与默认隐藏，不做物理删除任务（避免丢审计痕迹）。
6. 单用户无鉴权、仅 127.0.0.1 绑定的既有风险继续沿用，在 README 风险节保持声明。


---

## 八、B 批落地回填（2026-10-12 实施后实际结果）

- 新增文件：`app/recommendations.py`（存储/服务层，无路由、无规则引擎、无模型调用）、`tests/test_recommendations_store.py`（22 项）。
- 修改文件：`app/main.py`（仅 `init_db()` 内新增 `recommendations`、`recommendation_runs` 两表与索引的 `CREATE TABLE IF NOT EXISTS`，不动任何旧表）。
- 与计划的偏差：
  1. `dedup_key` **未建 UNIQUE 约束**，改为普通索引 `idx_recommendations_dedup`。原因：同一建议被拒绝超过 30 天冷却后需要允许重新生成新行，而历史拒绝行必须保留用于冷却判断；活跃去重与冷却由服务层查询保证（计划中的语义不变）。
  2. 证据指纹中 `ref_id` 为空时归一化为 -1、`value` 统一转字符串，保证排序去重不抛 `TypeError`，且证据顺序不影响去重结果。
- 测试：专项 `22 passed`；全量回归 `183 passed, 1 skipped, 5 warnings`（基线 161 + 新增 22）。
- 已覆盖：建表与重复 `init_db()` 幂等/旧数据保留、枚举与置信度校验、证据必填、密钥内容拒绝（400）、创建/查询/列表（默认隐藏 expired/rejected、优先级排序）、状态机全部合法与冲突路径（404/409）、删除、活跃去重、证据顺序无关、窗口不同则新建、拒绝 30 天冷却内外行为、惰性过期、run 生命周期/计数校验/错误脱敏。
- 未做（严格遵守批次边界）：未暴露任何 HTTP 路由，未实现规则引擎与 `collect_evidence/evaluate_rules/generate`，未调用模型，无前端改动。


---

## 九、C 批落地回填（2026-10-12 实施后实际结果）

- 新增文件：`app/recommendation_engine.py`（证据采集 / 确定性规则 / 模型改写 / 生成编排，与 B 批存储层职责分离）、`tests/test_recommendation_engine.py`（34 项）。
- 未新增 HTTP 路由、未改前端、未加依赖、未调用真实模型；异步模型调用以注入的假 callable 测试（环境无 pytest-asyncio，异步用例统一用 `asyncio.run` 同步驱动）。

### 证据结构（EvidenceBundle）

`window / metrics(复用 analytics.overview) / coverage / comparison(7、30 天窗口才有) / overdue_tasks / open_task_count / inactive_projects / stuck_goals / confirmed_profile / gaps`。

- 指标全部来自 `analytics.overview()` 与 `compare_periods()` 的真实快照，引擎不重算数字。
- 任务证据只取 `id/status/priority/due_date/project_id/goal_id`，**不读取任务标题**；项目/目标只取 id 与时间戳。
- 缺失语义显式区分：指标 `status=no_data/insufficient_data`；14 天窗口无对比 → `gaps` 记录 `comparison:unsupported_window:14d`；`profile_items` 表不存在 → `gaps` 记录 `profile_items:table_missing`（不假定 1.3.1 存在），读取异常 → `profile_items:read_failed`，均不影响主流程。
- 画像只取 `status='confirmed'` 条目（≤10 条、每条截断 300 字），且仅用于帮助模型润色措辞。

### 规则清单（ruleset@1，全部确定性）

| rule_code | 条件 | category |
| --- | --- | --- |
| `task.overdue_open` | 当前快照超期开放任务 >0；≥3 个升 high / 0.85，否则 normal / 0.7 | task |
| `task.completion_drop` | 等长窗口任务完成量显著下降（复用 analytics 显著性：样本≥5、降幅≥20%）；降幅≥40% 升 high；风险 medium | habit |
| `backlog.growing` | 窗口内新建≥5、完成/新建<0.6、当前开放≥5 | task |
| `project.inactive` | active 且创建早于窗口起点、窗口内 behavior_events 与 project_events 两路都无活动（每项目一条，上限 3）；明确"无记录≠放弃" | project |
| `goal.without_progress` | active 且创建早于窗口、窗口内无关联事件（上限 3） | goal |

单次最多 10 条，按优先级/规则码/证据稳定排序，保证同输入同输出。

### 模型改写与降级

- 模型只能回 `{draft_id,title,content,expected_benefit}`；类别/优先级/风险/置信度/证据均不经过模型，返回的额外字段被 Pydantic 忽略或拒绝。
- draft_id 越界/重复、结构非法、空 items、密钥模式 → 该条回退规则文案；整次调用异常（超时等）/非法 JSON/空输出 → 全部回退，run 记 `degraded`。
- 经校验的改写才标 `source='hybrid'`，未改写条目保持 `rule`；任何模型输出都再过 B 批存储校验与密钥过滤。

### 事务与计数语义

`generate`：先写 running 标记并 commit → 采集/规则 → 惰性过期 → 无候选记 `no_evidence` 返回；有候选则逐条走 B 批 `create_recommendation`（去重/冷却/过期全部生效），单事务提交；任何未预期异常先 rollback，再把 run 置 `failed`（错误经 `sanitize_run_error` 脱敏）并 commit，保证"数据库状态与运行记录一致"。重复调用第二次为 `created=0, duplicates=1`；拒绝冷却内 `suppressed_rejected=1`；40 天前的旧建议先过期再新建的路径已覆盖。全程不写 memories/profile_items（有专门断言）。

- 测试：引擎专项 `34 passed`；全量回归 `217 passed, 1 skipped, 5 warnings`；compileall 通过。
- 实施中修正的真实问题：① 过期重生成用例的截止日对"40 天前"尚未超期（测试数据口径，改为更早日期）；② 环境无 pytest-asyncio，异步用例改为 `asyncio.run` 驱动，未新增依赖。
- 遗留风险：14 天窗口拿不到周期对比（该窗口规则退化为快照类）；项目/目标停滞依赖事件被记录，系统外活动无法识别（文案已声明）；模型实际网络行为在 D/E 批接入真实端点前仅经注入测试覆盖。


---

## 十、D 批落地回填（2026-10-12 实施后实际结果）

- 新增文件：`app/static/recommendations.js`（「智能建议」页面）、`tests/test_recommendations_api.py`（14 项）。
- 修改文件：`app/main.py`（导入引擎 + 7 条建议路由 + 服务端模型开关）、`app/recommendations.py`（纯增量：列表加 `offset/priority`、新增 `count_recommendations`、runs 加 `offset`，未改既有语义）、`app/recommendation_engine.py`（新增进程内 asyncio 生成锁与 `GenerationInProgress`，未改规则/证据/降级契约）、`app/static/index.html`（侧栏入口、页面区块、navigate 分发、脚本引用）、`README.md`。

### 实际路由

- `GET /api/recommendations?status=&category=&priority=&limit=1..100&offset>=0` → `{items,total,limit,offset}`，默认隐藏 expired/rejected，非法枚举 400、分页越界 422。
- `GET /api/recommendations/{id}`（404 未知）。
- `POST /api/recommendations/generate`，body `{days:7|14|30, use_model:bool}`；返回 status/counts/created/window/gaps/run 及 `model_requested/model_used/model_disabled_by_server`；同进程并发生成 409。
- `POST /api/recommendations/{id}/accept|ignore|reject|reopen`（非法转移 409、未知 404），`DELETE /api/recommendations/{id}`。
- `GET /api/recommendation-runs?limit=&offset=`。

### 模型安全（默认关闭，服务端双闸门）

真实模型只有在 `LLM_API_KEY` 非空 **且** `RECOMMENDATIONS_USE_MODEL=1` 时才会被调用；客户端 `use_model=true` 无法单独开启，响应显式回传 `model_disabled_by_server`。测试环境两个变量均删除，且不注入任何 model_call，冒烟与测试链路确认不会发出网络请求（规则生成路径）。

### 并发一致性

同进程用模块级 asyncio.Lock 串行化手动生成，第二个并发请求直接 409 且不产生 run；文档与 README 明确该锁**不覆盖多进程部署**，跨进程由存储层 dedup_key 去重 + 拒绝冷却兜底（第二个进程最多得到 created=0/duplicates，而不会产生重复行或状态损坏）。

### 前端

vanilla JS IIFE，沿用全局 `esc()/api()/toast()`；列表分页 + 三类筛选、证据折叠（值为 null 显示"缺失，不是 0"）、窗口/覆盖率/风险/置信度/来源/有效期展示、采纳/忽略/拒绝/撤销/删除、最近 6 条 run、生成按钮防重复（请求中 disabled）、加载/空/错三态；动态按钮全部走 `data-rec-action` 事件委托，无内联 on*、无未转义 innerHTML 文本。

### 实际验证

- API 专项 `14 passed`；全量回归 `231 passed, 1 skipped, 5 warnings`；`compileall` 通过；`node --check recommendations.js` 通过。
- 浏览器冒烟（临时 SQLite + 临时 Uvicorn 8139 + Edge headless CDP，未触碰真实库）：HTTP 生成 2 条（`task.overdue_open`、`project.inactive`），`model_used=False`；页面 `page-recommendations` 激活、2 张卡片、证据折叠"2 条"、无错误条；点击采纳后标题徽章变"已采纳"；筛选 accepted 仅剩 1 条；`CONSOLE_ERRORS=[]`。

- 严格未做：未实现 E 批的专项加固/验收报告；未实现 1.3.4 反馈与结果追踪；建议不会被执行，不写任务/记忆/画像；未改 C 批规则、证据定义与模型输出契约。
- 已知风险：多进程/多 worker 下无分布式锁（已在代码注释与 README 说明，靠去重兜底）；前端分页为 offset 型，数据量很大时可后续改游标（当前规模足够）。

---

## 十一、E 批安全与质量验收回填（2026-10-12 实际结果）

### 11.1 审计范围与部署前提

- 重新审计 `app/main.py`、`app/recommendations.py`、`app/recommendation_engine.py`、`app/static/index.html`、`app/static/recommendations.js` 与 B/C/D 三批测试。
- 当前产品是**单用户、无登录、本地 loopback 部署**，因此不存在可验证的多租户 IDOR 攻击面；E 批未凭空引入用户体系。对应安全边界改为：服务只允许 loopback Host，并拒绝跨站状态变更。若未来绑定非 loopback 或支持多用户，必须先补登录鉴权与用户级数据隔离。
- 当前工作目录不是 Git 仓库；本轮未初始化 Git、未提交、未重置数据库。浏览器冒烟与业务测试均使用临时 SQLite 和临时 Edge profile。
- 收尾审计发现旧版 `tests/test_api.py` 在 pytest 先加载其他模块时只设置 `DATABASE_PATH`、未重绑定已导入的 `app.main.DB_PATH`，曾对真实 `data/personal_ai_os.db` 执行过一次幂等建表/建索引迁移；只读核查确认没有冒烟任务/项目/建议数据写入。E 批已修复该测试隔离缺口；修复后的完整测试前后真实库 mtime 保持 `2026-10-12 16:45:50` 不变。

### 11.2 已修复并回归验证的问题

| 类别 | 最小修复 | 回归覆盖 |
| --- | --- | --- |
| Loopback / DNS rebinding | 新增 Host 中间件，仅允许 `127.0.0.1`、`localhost`、`::1`；TestClient 合成 Host 单独放行；非法 Host 返回 400。 | API 测试 + 真实 Edge/HTTP 冒烟验证 400。 |
| CSRF / 跨站状态变更 | POST/PUT/PATCH/DELETE 校验同源 `Origin`；无 Origin 时校验 `Sec-Fetch-Site=same-origin`；跨站请求在路由前返回 403。无浏览器头的本地 API 客户端仍可用。 | API 测试 + 真实跨站请求返回 403。 |
| 跨进程重复活跃建议 | `_migrate_recommendations()` 在确认旧库无活跃重复后创建部分唯一索引 `idx_recommendations_active_dedup`；插入竞争转换为 duplicate，不创建第二条活跃建议。旧库已有重复时不删除用户数据、跳过索引并记录 warning。 | 存储层专项测试覆盖唯一索引、rejected 历史共存、旧重复数据安全迁移。 |
| 跨进程重复 running | 先恢复超时 running，再创建部分唯一索引 `idx_recommendation_runs_one_running`；并发启动返回 409 `generation_in_progress`。 | 存储层和生成引擎测试覆盖 409、恢复与失败收尾。 |
| 卡住运行 | 新增 `STALE_RUN_MINUTES=5` 与 `recover_stale_runs()`；生成开始时恢复超过 5 分钟的 running。取消任务时捕获 `CancelledError` 并将当前 run 标记 failed。 | 专项测试覆盖 stale recovery 与 cancellation。 |
| GET 隐式写库 | list/count 改为只读 SQL，逻辑过期项默认隐藏但不物理改状态；只有生成等受控写路径调用 `expire_stale()`。 | 存储/API 测试验证 GET 不改变状态，显式 expired 查询仍可查物理 expired。 |
| 状态竞争 | 状态流转改为 `WHERE id=? AND status=?` 条件更新；并发删除 404，并发状态变化 409；逻辑过期项拒绝状态操作。 | 存储/API 404/409 回归。 |
| 模型输出资源消耗与数值篡改 | 默认模型调用增加双闸门检查、20,000 字符响应上限、流式读取限制；标题/正文/收益中的数字必须是原数字的保序子序列，禁止新增/调换/篡改；同时识别“九成”“百分之九十”“三个”等中文数量表达，防止用中文数字绕过。 | mock 模型测试覆盖超时、异常、非法 JSON、空输出、虚构 ID、超长输出、新增数字、数字重排、阿拉伯数字与中文数字混排、`90%→九成/百分之九十`、密钥样式文本。 |
| 长事务与取消一致性 | running 标记在模型调用前 commit；模型网络调用不横跨 SQLite 写事务；异常路径 rollback 后写入脱敏 failed run。 | 引擎测试检查失败后建议计数和 run 状态一致。 |
| 前端按钮与资源错误 | 动态 ID 强制正整数；失败恢复按钮状态；删除/拒绝需确认；增加空内联 favicon 声明，消除与业务无关的 `/favicon.ico` 404 控制台噪声。 | 页面 wiring 测试 + Edge CDP 第二轮冒烟无 Network/Console/Runtime/Log 错误。 |
| 测试隔离 | `tests/test_api.py` 在模块级显式重绑定 `app.main.DB_PATH` 到临时数据库，teardown 恢复原全局值与环境变量，避免 pytest 导入顺序导致 `init_db()` 触碰真实库。 | 单独运行与完整全量测试均记录真实库 mtime，修复后 `REAL_DATABASE_TOUCHED=0`。 |

### 11.3 已检查但未发现问题的项目

- 建议标题、正文、证据、错误和运行记录在前端均通过全局 `esc()` 或 `textContent` 输出；动态按钮使用 `data-*` 与事件委托，没有把建议内容拼进内联事件、URL 或 HTML 属性。
- 生成链路只读已授权的本地统计、任务/项目/目标和已确认画像；不会写入 `memories`、`profile_items`、任务、项目或目标；模型只可改写程序候选的标题、正文和预期收益，不能更改证据、风险、优先级、置信度或数值。
- 运行错误经过长度截断和密钥模式过滤；运行记录不保存完整提示词、凭据或完整外部请求头。
- 分页 `limit<=100`、`offset>=0`；建议正文、证据、规则版本和错误字段在 Pydantic/SQL CHECK 层有边界。
- 规则生成不依赖模型；删除 `LLM_API_KEY` 和 `RECOMMENDATIONS_USE_MODEL` 后文件、任务、画像、统计与建议管理仍可使用。

### 11.4 已确认但不在本轮修复的限制

1. 系统仍为**无登录单用户本地应用**：loopback Host 与 Same-Origin 校验适合当前部署，不等同于公网多用户鉴权；不得在未加认证和授权前暴露到局域网/公网。
2. 进程内 `asyncio.Lock` 不协调多 worker；数据库唯一索引能防止重复活跃建议和重复 running 标记，但不提供分布式任务队列的完整调度语义。当前建议保持单实例部署。
3. 真实云端模型端点没有在本批联网调用；所有异常、超时、非法 JSON、超长响应和提示注入均通过注入 mock 验证。
4. 旧库如果已经存在活跃重复建议，迁移不会自动合并或删除用户数据，而是跳过唯一索引并通过 warning 暴露；需要人工审查后再决定清理方案。
5. 本轮未实现 1.3.4：没有建议评分、实际执行结果、预期/实际效果对比，也不会根据采纳结果更新长期偏好或画像。

### 11.5 本轮实际命令与结果

```powershell
$env:LLM_API_KEY=''; $env:RECOMMENDATIONS_USE_MODEL=''
.\.venv\Scripts\python.exe -m pytest tests/test_recommendations_api.py -q
# 18 passed, 5 warnings

.\.venv\Scripts\python.exe -m pytest tests/test_recommendations_store.py tests/test_recommendation_engine.py tests/test_recommendations_api.py -q
# 90 passed, 5 warnings

.\.venv\Scripts\python.exe -m pytest -q
# 251 passed, 1 skipped, 5 warnings in 74.82s; real DB mtime unchanged

.\.venv\Scripts\python.exe -m compileall -q app
node --check app\static\recommendations.js
# both passed
```

浏览器冒烟（2026-10-12 16:47 CST）：临时 SQLite、临时 Edge profile、Uvicorn `127.0.0.1:8145`、CDP `9228`；插入一条超期任务和一个窗口前创建的老项目。结果：页面打开、生成 2 条建议、run 为 `success/use_model=0/candidates=2/created=2`、第一条采纳后刷新为“已采纳”、`error_responses=[]`、`console=[]`、`exceptions=[]`、`log_entries=[]`；跨站 POST 403、非法 Host 400。

### 11.6 修改文件清单（E 批）

- `app/main.py`：loopback Host / Same-Origin 中间件；建议表和运行表的安全迁移；生成路由继续复用服务端模型闸门。
- `app/recommendations.py`：stale run 恢复、条件状态更新、跨进程 IntegrityError 转换、只读过期过滤、去重竞争处理。
- `app/recommendation_engine.py`：响应长度限制、默认模型调用双闸门、数字保序校验、模型调用前提交、取消收尾。
- `app/static/index.html`：空内联 favicon，避免浏览器自动请求 404；页面业务结构不变。
- `app/static/recommendations.js`：动态 ID 与按钮失败恢复加固。
- `tests/test_recommendations_store.py`、`tests/test_recommendation_engine.py`、`tests/test_recommendations_api.py`：E 批安全与竞争回归测试。
- `tests/test_api.py`：修复 pytest 模块导入顺序下的临时数据库隔离，避免初始化真实开发库。
- `README.md` 与本计划文档：回填真实审计、修复、限制和实测结果。

