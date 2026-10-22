# Personal AI OS 1.3.2：行为与习惯统计 — 现状盘点与实施计划

- 创建日期：2026-10-12（Asia/Shanghai）
- 当前阶段：**1.3.2-A/B/C/D 均已实现并通过测试；行为事件 → 确定性统计 → 周期对比 → 只读 API/页面闭环完成**
- 范围纪律：本文件创建阶段不修改业务代码、不新增数据表、不执行破坏性操作。
- 盘点基线（2026-10-12 本机复跑）：`130 passed, 1 skipped, 5 warnings`。
- 1.3.2-A 新增测试后全量结果：`140 passed, 1 skipped, 5 warnings`（最终回归见文末落地记录）。

## 1. 本轮结论

当前系统已经具备长期记忆、文档分块检索、项目 / 目标 / 任务 CRUD、个人画像和连接器框架，但**还没有统一的行为事件模型**。

真实本地库在盘点时的数据量如下：

| 数据 | 数量 | 对行为统计的意义 |
| --- | ---: | --- |
| `profile` | 1 | 可作为个人档案上下文，但不能直接推出行为模式。 |
| `memories` | 2 | 已确认事实数量很少，不能据此做习惯判断。 |
| `conversations` / `messages` | 33 / 60 | 只能证明有系统交互；不能等同工作或学习时长。 |
| `documents` / `document_chunks` | 0 / 0 | 当前无法计算知识入库趋势。 |
| `projects` | 0 | 当前无项目状态样本。 |
| `project_events` | 0 | 当前无手动项目事件样本。 |
| `goals` / `tasks` | 0 / 0 | 当前无法计算任务完成率、延期率、目标推进情况。 |
| `profile_items` / revisions | 0 / 0 | 当前无画像确认 / 拒绝行为样本。 |
| `data_sources` / `sync_logs` / `source_items` | 0 / 0 / 0 | 当前只能开发统计能力，无法从真实库得出连接器使用习惯。 |

因此，1.3.2 的第一原则是：**先建立有明确来源和时间戳的行为事件，再做确定性统计；没有记录只能标记为“数据不足/无观测”，不能当成零，更不能推断人格或稳定习惯。**

## 2. 已检查的现有能力

### 2.1 已有数据源

- `documents` / `document_chunks`：有 `created_at`、`updated_at`、来源、哈希、处理状态和分块来源，可统计“资料入库数量”。
- `projects`：有 `status`、`created_at`、`updated_at`，但没有自动状态变更历史。
- `project_events`：已有 `progress`、`decision`、`issue`、`learn`、`note` 五类手动事件和 `created_at`，可作为项目活动来源。
- `goals`：有 `kind`、`status`、`target_date`、时间戳，但没有状态变更时间线。
- `tasks`：有 `status`、`priority`、`due_date`、`completed_at`、`created_at`、`updated_at`；只能可靠统计当前状态、创建时间和最后一次完成时间。
- `sync_logs`：有同步开始 / 结束时间、状态、游标和 created/updated/unchanged/skipped/deleted/failed 计数，可统计连接器运行健康度。
- `messages`：有消息角色和时间，但正文可能包含敏感内容；第一版不把消息正文写入行为事件。
- `profile_item_revisions`：可用于未来统计画像确认率，但不是 1.3.2 第一批核心指标。

### 2.2 现有缺口

1. 没有 `behavior_events` 表，也没有统一事件记录接口。
2. `tasks.updated_at` 会被任意更新覆盖，无法重建完整状态迁移历史。
3. 任务从 `done` 改回 `doing` 时，当前代码会清空 `completed_at`，历史完成动作会丢失。
4. `goals` / `projects` 没有状态历史字段；不能推断状态变化发生的准确时间。
5. `project_events` 是用户手动记录，不是自动埋点，不能保证覆盖所有项目活动。
6. `sync_logs` 是系统运行日志，不应与个人行为事件混为一类。
7. 统计口径、时区、日期边界、样本不足和缺失数据规则尚未实现。

## 3. 指标可计算性审计

| 候选指标 | 当前是否可算 | 正确口径 | 不可越界的解释 |
| --- | --- | --- | --- |
| 每日新增资料数 | 表结构可算，真实库当前无数据 | 按 `documents.created_at` 分桶 | 只能说明资料入库数量，不等于学习时长或效率。 |
| 每日项目活动数 | 表结构可算，真实库当前无数据 | `project_events.created_at` + 后续项目生命周期事件 | 只说明系统观测到项目活动。 |
| 任务新增数 | 表结构可算，真实库当前无数据 | 按 `tasks.created_at` 或 `task.created` 事件 | 不代表实际完成工作量。 |
| 任务完成数 | 表结构可算，真实库当前无数据 | 按 `completed_at` 或 `task.completed` 事件 | 不反映任务难度和耗时。 |
| 任务完成率 | 只能按明确公式算 | 见第 7 节，需要同时返回分母和样本量 | 不能把补录旧任务、周期外创建任务混为一个模糊百分比。 |
| 平均 / 中位完成耗时 | 后续可算 | `completed_at - created_at`，仅统计两个时间都有效的任务 | 无法排除任务中途暂停，不能等同纯工作时长。 |
| 目标进度 | 只能算状态与关联任务概况 | active/done/dropped、关联任务完成情况 | 不能替代用户定义的真实衡量标准。 |
| 同步成功率 | 表结构可算，真实库当前无数据 | 成功同步日志 / 已结束同步日志 | 只反映连接器健康度，不是个人习惯。 |
| 活跃日期分布 | 事件模型落地后可算 | 不同本地日期下至少一条允许统计的事件 | 只能说明“系统观察到活动”，不能说明当天没有线下活动。 |
| 学习效率 / 固定习惯 | 当前不可算 | 需要更长周期、明确学习事件和足够样本 | 不得由文档数、聊天数或单次行为推断。 |

## 4. 连接器与数据安全边界审计

现有连接器基础总体符合轻量框架方向：

- 连接器注册不等于授权；数据源必须单独创建。
- 调度默认关闭，逐源启用，后台串行执行，有失败退避。
- 同步失败不会删除既有文档；鉴权失败会标记 revoked。
- 外部删除只影响同一 `data_source_id` + `external_id` 的资源。
- GitHub 目标主机硬编码为 `api.github.com`，不跟随重定向。
- 本地目录连接器限制深度、文件数量和扩展名，并处理符号链接逃逸。
- HTTP API 连接器默认要求 HTTPS，禁止私网、环回、链路本地、多播和保留地址，不跟随重定向，有超时、响应大小和条目数上限。

但在扩大远程数据源前，建议独立处理以下安全项：

1. **DNS rebinding / TOCTOU**：HTTP 连接器先解析并校验 IP，随后 `httpx` 会再次解析；攻击者可在两次解析间切换 IP。应通过自定义 transport / 连接到已校验 IP 并保留 Host/TLS 信息实现“解析结果固定”。
2. **响应大小限制时机**：当前在响应读完后检查 `len(resp.content)`。应改为流式读取并提前中断，避免远端先发送超大响应体造成内存压力。
3. **分页参数**：HTTP API 连接器第一版只读取单页列表，需要明确 `next_cursor` / URL 模板 / 页码上限，不能信任远端无限分页。
4. **公网 URL 范围**：即使阻断内网，用户配置的公网目标仍可能是非预期服务；后续可增加域名 allowlist 和删除 / 撤销时的数据策略说明。

上述安全项不阻塞 1.3.2-A，因为行为统计只读本地库且不新增远程连接；但应在 3.5.3 后续增强或接入新连接器前优先修复前两项。

## 5. 建议的数据模型（1.3.2-A）

新增纯追加式轻量表 `behavior_events`，不保存消息正文、任务详情、文档正文或密钥。

```sql
CREATE TABLE IF NOT EXISTS behavior_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    data_source_id INTEGER REFERENCES data_sources(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    dedup_key TEXT UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

建议索引：

- `(occurred_at, event_type)`：时间窗口与趋势查询。
- `(event_type, occurred_at)`：单指标查询。
- `(subject_type, subject_id)`：追溯来源记录。
- `(project_id)`、`(task_id)`、`(goal_id)`、`(data_source_id)`：关联筛选。
- `(source_type, source_id)`：删除 / 回填 / 去重排查。

### 5.1 字段规则

- `event_type`：小写点分命名，例如 `task.created`、`task.completed`。
- `occurred_at`：事件真实发生时间，统一存 UTC ISO-8601；展示和按日分桶时按用户指定时区转换。
- `subject_type` / `subject_id`：主关联对象，允许源记录删除后置空。
- `source_type`：`system`、`api`、`backfill` 等；连接器健康统计仍以 `sync_logs` 为准。
- `dedup_key`：稳定幂等键，允许 NULL；同一键重复写入使用 `INSERT OR IGNORE`。
- `metadata_json`：只存枚举、布尔值、计数、状态等非敏感上下文，不存正文；长度限制并复用密钥模式检查。

### 5.2 第一批事件类型

| 事件 | 类型 | 写入时机 |
| --- | --- | --- |
| 资料创建 | `document.created` | 文本新增、文件解析成功、连接器入库成功后。 |
| 项目创建 | `project.created` | 创建项目后。 |
| 项目更新 | `project.updated` | 项目内容更新后。 |
| 项目状态变化 | `project.status_changed` | 状态实际变化后，metadata 记录 old/new。 |
| 项目事件记录 | `project_event.recorded` | 手动新增 progress/decision/issue/learn/note 后。 |
| 目标创建 | `goal.created` | 创建目标后。 |
| 目标更新 | `goal.updated` | 目标内容更新后。 |
| 目标状态变化 | `goal.status_changed` | active/done/dropped 等实际变化后。 |
| 任务创建 | `task.created` | 创建任务后。 |
| 任务更新 | `task.updated` | 非状态字段更新后。 |
| 任务完成 | `task.completed` | 状态从 todo/doing 变为 done。 |
| 任务重开 | `task.reopened` | 状态从 done 变为 todo/doing。 |
| 任务取消 | `task.cancelled` | 状态变为 cancelled。 |

第一版不自动记录聊天正文，也不生成 `insights`、`recommendations` 或反馈表；这些属于 1.3.3–1.3.4。

### 5.3 历史数据回填

迁移只建表，不自动插入事件。提供一个显式、幂等的 backfill 服务 / 内部入口：

- 项目：可回填 `project.created`；当前状态不能推断状态变化时间。
- 项目事件：可按 `project_events.created_at` 回填。
- 目标：可回填 `goal.created`；done/dropped 若无时间戳，不编造完成时间。
- 任务：可回填 `task.created`；若 `completed_at` 存在，可回填一次 `task.completed`。
- 资料：可回填 `document.created`，并区分 manual/upload/connector。
- 同步日志：不写入行为事件，统计服务直接查询 `sync_logs`。
- 聊天消息：第一批不回填，避免把交互量误解释为工作 / 学习行为。

回填键示例：`backfill:tasks:12:created`、`backfill:tasks:12:completed`。重复执行不得重复计数。

删除策略：源任务 / 项目等被删除时，事件外键置空，保留最小时间戳和事件类型用于聚合；事件不保留标题、正文。后续 1.6 再提供完整的活动记录导出与清除策略。

## 6. 模块与 API 规划

新增独立模块，避免继续膨胀 `app/main.py`：

- `app/behavior_events.py`：事件类型、校验、幂等写入、回填。
- `app/analytics.py`：纯函数统计、时间窗口、时区分桶、周期对比、数据覆盖率。
- `tests/test_behavior_events.py`：事件写入、迁移、回填、删除和幂等测试。
- `tests/test_analytics.py`：统计公式、时区、边界、缺失值、样本不足测试。

第一批 API 可在 1.3.2-A 末尾仅暴露事件查询和显式回填：

- `GET /api/behavior/events?event_type=&from=&to=&limit=`：查询事件，不返回敏感正文。
- `POST /api/behavior/backfill`：显式执行幂等回填，返回各类插入 / 跳过数量。

1.3.2-B/D 再增加：

- `GET /api/analytics/overview?days=7&timezone=Asia/Shanghai`
- `GET /api/analytics/trends?metric=...&days=30&timezone=...`
- `GET /api/analytics/comparison?window=7|30&timezone=...`
- `GET /api/analytics/coverage`

所有统计接口在模型不可用时必须正常工作；1.3.2 不调用云端模型。

## 7. 统计公式与输出约束

### 7.1 时间窗口

- API 接收 IANA 时区，例如 `Asia/Shanghai`；非法时区返回 400。
- 默认时区为 `Asia/Shanghai`（本机用户环境），内部仍以 UTC 存储。
- 日趋势按用户时区切分自然日。
- 周期对比使用等长窗口：最近 7 天对前 7 天，最近 30 天对前 30 天。
- 自定义窗口必须包含起止时间，且设置最大跨度（建议 366 天）。

### 7.2 任务指标

- `tasks_created`：`occurred_at` 落在窗口内的 `task.created`。
- `tasks_completed`：`occurred_at` 落在窗口内的 `task.completed`。
- `net_completion_ratio = tasks_completed / tasks_created`：可大于 1，用于观察本期是否消化往期积压，不命名为传统完成率。
- `created_cohort_completion_rate = 窗口内创建且当前状态为 done 的任务数 / 窗口内创建任务数`：适合评估最终完成，但近期窗口可能滞后。
- `open_backlog_count`：统计生成时 todo/doing 的任务数。
- `overdue_open_count`：当前状态为 todo/doing，且 `due_date` 早于用户时区当前日期。
- `completion_duration_minutes`：仅对有可信 created/completed 时间且 completed >= created 的任务计算平均值和中位数。

每个指标必须返回：`value`、`unit`、`numerator`、`denominator`、`sample_size`、`status`、`definition_version`、`generated_at`。

### 7.3 样本状态

- `ok`：达到最低样本要求。
- `insufficient_data`：有记录但样本不足，不输出强结论。
- `no_data`：窗口内无观测。
- `partial_data`：有部分来源，但关键来源缺失或时间覆盖不完整。

周期变化只有在当前或基线至少有最小样本（建议 n >= 5）且变化比例至少 20% 时才标记为显著；否则只展示数值变化。即使显著，也只描述“观测到变化”，不自动归因。

## 8. 迭代计划

### 1.3.2-A：行为事件基础设施（已完成，2026-10-12）

交付：

1. 新增 `behavior_events` 表和幂等迁移。
2. 新增 `app/behavior_events.py`。
3. 在项目、目标、任务、项目事件、文档入库事务中写入事件。
4. 实现显式、幂等 backfill。
5. 增加事件查询 API 和基础测试。
6. 不做图表、不做模型解释、不做建议。

验收：

- 事件可查询、可去重、可按时间 / 类型过滤。
- 业务记录与事件在同一事务中写入，失败时一起回滚。
- 重复回填不重复计数。
- 不保存正文、密钥或不必要敏感信息。
- 旧库迁移不丢数据，现有 130 个测试继续通过。

### 1.3.2-B：纯程序统计引擎（已完成，2026-10-12）

交付时间窗口、日分桶、资料新增、项目活动、任务流、同步健康和覆盖率计算。所有逻辑用确定性单元测试验证，不依赖大模型。

### 1.3.2-C：周期对比与异常变化（已完成，2026-10-12）

实现 7/30 天等长窗口对比、变化率、零分母处理和样本不足标记；不输出原因推断。

### 1.3.2-D：分析 API 与页面（已完成，2026-10-12）

新增分析 API 和前端“行为洞察”页：趋势表、周期对比、数据覆盖率、统计口径说明；完整处理空状态、加载中、错误和非法时区。

## 9. 测试计划

### 1.3.2-A

- 新库自动创建 `behavior_events`，旧库连续两次迁移不报错且数据计数不变。
- 创建 / 更新 / 完成 / 重开 / 取消任务产生正确事件。
- 非状态更新不产生 `status_changed`。
- 项目 / 目标状态变化记录 old/new。
- 项目事件、文档创建事件可追溯到来源 ID。
- 回填对旧项目、项目事件、目标、任务、文档幂等。
- 不为缺失的完成时间编造事件。
- 删除任务 / 项目后事件聚合仍可保留，但源 ID 按策略置空。
- metadata 过大或疑似密钥被拒绝。
- 非法 event_type、时间格式、查询参数返回 400/422。
- 全程不联网、不调用模型。

### 1.3.2-B/C

- UTC 与 Asia/Shanghai 日期边界。
- 空窗口、部分覆盖、零分母、样本不足。
- 重复事件 / 幂等事件不重复统计。
- 平均值、中位数、完成率、净完成比、超期数公式正确。
- 7 天与前 7 天、30 天与前 30 天窗口等长且不重不漏。
- 同步失败和外部删除不被误算为个人行为。
- 统计结果不包含文档 / 消息正文。

### 回归

- `.\.venv\Scripts\python.exe -m pytest -q`
- 重点回归：文档删除、连接器同步、任务 CRUD、画像 CRUD、聊天本地演示模式。

## 10. 明确不做

- 不把聊天数量解释为学习 / 工作时长。
- 不因一次任务完成或延期推断稳定人格。
- 不自动把统计结果写入长期记忆或个人画像。
- 不调用大模型生成统计数值。
- 不创建建议、建议反馈、自动提醒或 Agent 操作。
- 不接入新的第三方平台。
- 不扫描磁盘；只使用系统内已有且由用户主动创建 / 授权的数据。

## 11. 批准门槛

进入 1.3.2-A 前，需要明确确认：

1. 同意新增 `behavior_events` 表。
2. 同意后续在项目 / 目标 / 任务 / 文档写操作的同一事务中自动记录最小事件。
3. 同意历史回填采用显式触发、幂等执行，而不是启动时自动写入。
4. 同意第一批统计只报告可验证指标，不生成习惯结论或个性化建议。


## 12. 1.3.2-A 落地回填（2026-10-12）

### 12.1 实际交付

- 新增 `app/behavior_events.py`：事件常量、事件/来源/主体校验、ISO-8601 时间归一化、metadata 密钥过滤、幂等写入、显式回填和事件查询。
- 修改 `app/main.py`：
  - 启动幂等迁移新增 `behavior_events` 表及时间、类型、主体、项目、目标、任务、数据源索引。
  - 在文档、项目、项目事件、目标、任务写入路径的同一事务中记录最小事件。
  - 新增 `GET /api/behavior/events` 与 `POST /api/behavior/backfill`。
- 新增 `tests/test_behavior_events.py`，共 10 个测试。
- 更新 `README.md` 与阶段计划。

### 12.2 已验证行为

- 新库与旧库迁移均会创建 `behavior_events`，连续执行 `init_db()` 不重复建表、不丢旧数据。
- 迁移只建表，不会在启动时自动回填历史事件。
- 任务创建、完成、重开、取消以及项目/目标状态变化能记录 old/new 状态。
- 普通更新只在字段实际变化时记录 updated；状态变化优先记录更具体的生命周期事件。
- 事件 metadata 不包含文档/任务/项目事件正文，不包含文件名以外的业务正文；当前实现也不保存文件名。
- metadata 命中 Bearer token、API key、password/secret/token 等模式时拒绝写入。
- 文档、项目、项目事件、目标、任务的历史可确定事件可显式回填；重复回填通过 dedup key 和已有事件检查跳过。
- 不会为已完成的旧项目/目标编造 `status_changed` 事件；只回填可由 `created_at` / `completed_at` 确定的 created/completed。
- 删除项目后，事件时间线保留，项目关联置空；同一项目下任务事件的 `project_id` 置空，任务事件本身保留。
- 事件服务异常时，同事务中的业务记录回滚。

### 12.3 API

- `GET /api/behavior/events?event_type=task.created&from=2026-10-01T00:00:00%2B08:00&to=2026-10-12T23:59:59%2B08:00&limit=100`
- `POST /api/behavior/backfill`

### 12.4 测试

- 专项测试：`10 passed`。
- 全量回归：`140 passed, 1 skipped, 5 warnings`。

### 12.5 未实现 / 下一批

- 尚未实现统计引擎、趋势分桶、周期对比、数据覆盖率、前端页面。
- 尚未自动记录画像审核、记忆审核、连接器同步等事件；连接器健康仍应直接读取 `sync_logs`。
- 尚未记录聊天事件，避免把系统交互量误当成工作/学习行为。
- 下一批 1.3.2-B 应新增 `app/analytics.py`，只做确定性程序统计，不调用大模型。


## 13. 1.3.2-B 落地回填（2026-10-12 09:52 CST）

### 13.1 实际交付

- 新增 `app/analytics.py`：
  - `TimeWindow` 与 `build_window()`：1..366 天窗口、UTC/Asia/Shanghai/UTC±HH:MM 校验、自然日起止时间。
  - `overview()`：统一输出窗口、每日序列、指标和覆盖率。
  - 行为事件计数：资料创建、项目活动、目标创建、任务创建/完成/重开/取消。
  - 任务快照：当前开放任务数、当前超期开放任务数。
  - 完成耗时：窗口内完成任务的平均分钟数、中位分钟数；忽略完成时间早于创建时间的异常数据。
  - 连接器健康：读取 `sync_logs`，统计同步尝试数和 success/(success+failed)。
  - 样本状态：`no_data`、`insufficient_data`、`ok`。
- 新增 `tests/test_analytics.py`，共 10 个确定性测试。
- 更新 `README.md` 与本计划。

### 13.2 明确口径

- 每日序列按用户指定时区的自然日分桶，而不是 UTC 自然日。
- `net_completion_ratio = task.completed / task.created`，可大于 1，表示本期完成了历史积压；它不是传统“任务完成率”。
- 开放任务和超期任务是当前快照，不受统计窗口内事件数量限制。
- 完成耗时只使用窗口内 `completed_at` 落在区间内的已完成任务；它不能等同于纯工作时长。
- running 状态的同步计入 attempts，但不计入成功率分母。
- 无行为事件不等于用户没有线下活动，只代表系统在窗口内没有观测。

### 13.3 测试结果

- 红灯：`app.analytics` 最初不存在，测试收集阶段失败；新增快照指标时先出现缺失 KeyError，随后实现。
- 专项测试：`10 passed, 4 warnings`。
- 编译检查：`compileall: OK`。
- 全量回归：`150 passed, 1 skipped, 5 warnings in 29.25s`，退出码 0。

### 13.4 未实现 / 下一批

- 尚未实现最近 7 天 vs 前 7 天、最近 30 天 vs 前 30 天的周期对比。
- 尚未实现变化率、显著性阈值、方向标记和样本不足保护。
- 尚未暴露分析 HTTP API 或前端仪表盘（计划在 1.3.2-D）。
- 尚未支持任意 IANA 时区；如后续需要，可显式添加 `tzdata` 依赖后扩展。
- 不调用大模型，不归因，不生成建议，不写入长期记忆或个人画像。

## 14. 1.3.2-C 落地回填（2026-10-12 10:13 CST）

### 14.1 实际交付

- 修改 `app/analytics.py`：新增比较定义版本、窗口负载、连接器日志计数、比较状态、计数比较和 `compare_periods()`。
- 修改 `tests/test_analytics.py`：在 1.3.2-B 的 10 个概览测试上新增 5 个周期对比测试，共 15 个专项测试。
- 更新 `README.md`、本计划和 `docs/plans/phase-4-personalization-engine.md`。
- 无数据库迁移、无新增 HTTP API、无前端改动。

### 14.2 窗口与公式

- 仅支持 `window_days=7` 或 `30`；其他值抛出明确错误。
- 当前窗口按指定时区包含最近 N 个自然日（含今天），结束于当前窗口最后一天的次日本地午夜；前一窗口为紧邻、等长、不重叠的 N 天半开区间。
- `delta = current - previous`；`direction` 为 `up/down/flat`。
- previous 为 0 时 `percent_change=None`；current 为 0 且 previous 大于 0 时 `percent_change=-1.0`；其他情况按 `delta / previous` 并保留 4 位小数。
- `running` 同步日志计入 attempts，不计入 successes/failures，也不进入成功率分母。

### 14.3 显著性与状态

- 两侧都为 0：`status=no_data`、`significant=false`。
- 两侧最大值小于 5：`status=insufficient_data`、`significant=false`。
- 非零基线：最大样本数至少 5，且绝对变化率至少 20%，才标记显著。
- 零基线上升：仅当当前值至少 5 时标记显著；降到 0：仅当基线至少 5 时标记显著。
- 显著只表示数值变化达到本版阈值，不表示原因、习惯、人格特征或因果关系。

### 14.4 测试结果

- 专项命令：`.\.venv\Scripts\python.exe -m pytest tests/test_analytics.py -q`
- 专项结果：`15 passed, 4 warnings`。
- 全量命令：`.\.venv\Scripts\python.exe -m pytest -q`
- 全量结果：`155 passed, 1 skipped, 5 warnings in 38.35s`。
- 编译检查：`.\.venv\Scripts\python.exe -m compileall -q app` → `compileall: OK`。

### 14.5 未实现 / 下一批

- 尚未暴露 `GET /api/analytics/...`，也没有前端“行为洞察”页；这是 1.3.2-D 的范围。
- 尚未进行原因解释、异常洞察文案生成、建议生成或提醒。
- 不把周期对比结果写入长期记忆或个人画像。
- 后续 1.3.2-D 应新增只读分析 API 与页面，明确统计口径、空状态、错误状态和时区参数，不自动扩展为建议引擎。

## 15. 1.3.2-D 落地回填（2026-10-12 10:27 CST）

### 15.1 实际交付

- 修改 `app/main.py`：导入 `analytics`，新增两个只读路由：
  - `GET /api/analytics/overview?days=1..366&timezone=...`
  - `GET /api/analytics/comparison?window_days=7|30&timezone=...`
- 新增 `app/static/analytics.js`：独立行为洞察页面脚本，负责加载/刷新、错误和空数据展示、概览卡片、每日趋势表、周期对比表、覆盖率与统计口径。
- 修改 `app/static/index.html`：侧栏新增「行为洞察」，新增 `page-analytics` 区块、导航调用和静态脚本引用。
- 新增 `tests/test_analytics_api.py`，共 6 个 API/前端挂载测试。
- 更新 `README.md`、本计划和 `docs/plans/phase-4-personalization-engine.md`。
- 无数据库迁移、无新增表、无写入接口、无模型调用。

### 15.2 API 行为与错误口径

- `overview` 使用 1.3.2-B 的确定性 `analytics.overview()`；FastAPI 负责 `days` 的 1..366 边界，非法时区由统计模块转换为 400。
- `comparison` 使用 1.3.2-C 的 `analytics.compare_periods()`；`window_days` 仅允许 7 或 30，其他整数返回 400，类型错误由 FastAPI 返回 422。
- 接口只返回统计值、样本状态、时间窗口和来源覆盖标记，不返回任务标题、文档正文、消息正文或画像内容。

### 15.3 前端验证点

- 空数据库时显示 0 值和 `no_data/无观测`，不把无事件解释为用户没有活动。
- 支持概览窗口（7/30）、对比窗口（7/30）和时区（Asia/Shanghai、UTC、固定偏移）切换。
- 页面默认不加载，只有导航到「行为洞察」时调用只读接口。
- 所有动态文本通过现有 `esc()` 转义；指标名称为本地固定枚举，接口数据只写入文本节点、表格和数值。
- 临时服务 + Windows 系统 Edge headless DevTools Protocol 冒烟结果：`active=page-analytics`、概览卡 6 个、对比行 10 行、错误区域隐藏、定义版本显示 `1.3.2-B / 1.3.2-C`。

### 15.4 测试结果

- 红灯：新增 6 个测试最初全部失败，其中 5 个为 404（路由不存在），1 个为页面入口/脚本不存在。
- 后端与前端实现后专项：`21 passed, 5 warnings`（`tests/test_analytics.py` 15 个 + `tests/test_analytics_api.py` 6 个）。
- 静态脚本语法：`node --check app/static/analytics.js` → OK。
- Python 编译：`.\.venv\Scripts\python.exe -m compileall -q app` → `compileall: OK`。
- 全量回归：`161 passed, 1 skipped, 5 warnings in 23.64s`。
- Edge 运行时冒烟：通过，使用临时数据库，未污染真实库。

### 15.5 未实现 / 下一阶段

- 1.3.2 已闭环；尚未进入 1.3.3。
- 未生成自然语言洞察、原因解释、个性化建议、建议反馈或主动提醒。
- 未将统计结果写入长期记忆或个人画像。
- 未引入图表库；当前使用表格和指标卡，符合无构建步骤前端约束。
- 项目 venv 未安装 Playwright 包；本次用系统 Edge DevTools Protocol 做真实浏览器冒烟，未把 Playwright 加入运行依赖。

