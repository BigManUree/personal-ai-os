# Personal AI OS 1.3.5 主动提醒与复盘 — A 批现状审计与规则设计

> **批次：1.3.5-A（只审计、只设计、只写计划文档）**  
> **日期：2026-10-12**  
> **结论：本批未修改业务代码、未新增数据库表、未启动调度器、未调用真实大模型、未触碰真实用户数据。**  
> **部署前提：本地 loopback、单实例、单用户、无登录。**

---

## 0. 本批目标与纪律

1.3.5 的目标不是让系统“催促用户”，而是在有可靠数据、明确规则、可取消和可去重的前提下，生成可解释、可延期、可取消、可过期的主动提醒，并以每日/每周复盘帮助用户回顾已观测事实。

A 批只确定：

- 现有数据源和时间语义；
- 哪些提醒可以可靠生成，哪些不能；
- 提醒规则、状态机、去重和恢复机制；
- 每日/每周复盘的数据口径和草稿生命周期；
- 后续 B～F 的数据模型、依赖和验收标准。

A 批不做：

- 不创建 `reminders`、`review_reports` 等表；
- 不修改 `app/main.py` 启动流程；
- 不新增后台线程、定时任务或依赖；
- 不发送邮件、短信、手机推送或外部通知；
- 不自动创建任务、修改目标、写入 `memories` 或 `profile_items`；
- 不调用真实模型；
- 不自动开始 B 批。

---

## 1. 审计证据与当前架构

### 1.1 数据库连接和初始化

证据：`app/main.py:117-126`、`app/main.py:277-642`。

- `db()` 为每次操作创建独立 SQLite 连接：
  - `sqlite3.connect(DB_PATH)`；
  - `conn.row_factory = sqlite3.Row`；
  - 每个连接执行 `PRAGMA foreign_keys = ON`；
  - 正常退出时 commit，最终关闭。
- `init_db()` 使用 `CREATE TABLE IF NOT EXISTS` 和幂等迁移函数，在 FastAPI startup 中执行。
- 当前真实项目仍为单 SQLite 库，默认路径是 `data/personal_ai_os.db`，测试必须同时设置 `DATABASE_PATH` 和已导入模块的 `app.main.DB_PATH`。

对 1.3.5 的影响：

- 新表和索引必须继续走幂等初始化；
- 所有新连接不能假设外键默认开启；
- 调度器不能只靠进程内状态，必须用数据库唯一键和条件更新保证重启/重复 tick 不重复创建提醒；
- 测试必须继续使用临时库，不允许只设置环境变量。

### 1.2 已有的启动/关闭生命周期

证据：`app/main.py:644-659`、`app/connector_scheduler.py:1-171`。

当前已有一个**数据源同步专用**后台调度器：

- startup：
  - `init_db()`；
  - 创建模块级 `ConnectorScheduler`；
  - 调用 `start()`。
- shutdown：
  - cancel 后台任务；
  - 等待退出；
  - 将实例置空。
- `ConnectorScheduler`：
  - 单进程 asyncio task；
  - 默认 30 秒轮询；
  - 数据源同步必须逐源、串行执行；
  - `_tick_lock` 只防止同进程重叠 tick；
  - 无分布式锁、无 cron 表达式、无 webhook；
  - due 查询依据 `data_sources.next_sync_at <= UTC now`；
  - 失败按指数退避更新下一次时间。

对 1.3.5 的影响：

- 可以复用其“纯时间函数 + 可注入 runner + 串行 tick + shutdown 可取消”的设计方式；
- 不应直接把提醒逻辑塞进连接器调度器；
- 提醒调度应另建独立模块，例如 `app/reminder_scheduler.py`；
- 进程内锁不能保证多实例完全串行，提醒幂等必须落数据库；
- 本阶段仍不支持多进程/多设备同时运行。

### 1.3 任务、项目与目标

证据：`app/main.py:39-44`、`app/main.py:374-419`、`app/main.py:1367-1444`、`app/main.py:1699-1796`。

#### 项目

`projects` 字段：

- `id`
- `name`
- `description`
- `tech_stack`
- `repo_url`
- `status`: `planning/active/paused/completed/archived`
- `created_at`
- `updated_at`

另有 `project_events`：

- `event_type`: `progress/decision/issue/learn/note`
- `content`
- `created_at`
- 外键关联项目，删除项目时级联。

#### 目标

`goals` 字段：

- `id`
- `title`
- `description`
- `kind`: `long_term/milestone`
- `status`: `active/done/dropped`
- `measure`
- `target_date`：**仅 `YYYY-MM-DD` 日期，无具体时间、无时区**
- `project_id`
- `created_at`、`updated_at`

#### 任务

`tasks` 字段：

- `id`
- `title`
- `detail`
- `status`: `todo/doing/done/cancelled`
- `priority`: `low/normal/high`
- `due_date`：**仅 `YYYY-MM-DD` 日期，无具体时间、无时区**
- `project_id`、`goal_id`
- `completed_at`
- `created_at`、`updated_at`

任务写入规则：

- `due_date` 由 `_validate_iso_date()` 校验为 `YYYY-MM-DD`；
- 新建任务若状态为 `done`，`completed_at=now`；
- 更新任务时，每次保存都会刷新 `updated_at`；
- `todo/doing -> done` 记录 `task.completed`；
- `done -> todo/doing` 记录 `task.reopened`；
- 非取消状态变为 `cancelled` 记录 `task.cancelled`；
- 普通字段或状态变化记录 `task.updated`。

对 1.3.5 的影响：

- **截止时间提醒只能精确到本地日期，不能声称精确到几点几分。**
- 任务完成/取消可作为提醒取消条件。
- 任务停滞不能只看“用户没打开应用”，必须基于任务状态和已记录活动。
- `updated_at` 会被任意编辑刷新，不能单独证明真实执行；停滞提醒只能表述为“系统未观测到任务记录更新”。
- A 批建议第一版只对 `doing` 任务生成停滞提醒；不对长期 `todo` 任务推断“拖延”。

### 1.4 行为事件

证据：`app/main.py:504-526`、`app/behavior_events.py:15-58`、`app/behavior_events.py:92-208`。

`behavior_events` 是 append-only 最小事件表：

- `event_type`
- `occurred_at`
- `subject_type`
- `subject_id`
- `project_id`、`goal_id`、`task_id`、`data_source_id`
- `source_type`: `system/api/backfill`
- `dedup_key` 唯一
- `metadata_json`

现有任务相关事件：

- `task.created`
- `task.updated`
- `task.completed`
- `task.reopened`
- `task.cancelled`

建议相关事件：

- `recommendation.accepted`
- `recommendation.ignored`
- `recommendation.rejected`
- `recommendation.reopened`
- `recommendation.feedback_saved`
- `recommendation.feedback_deleted`
- `recommendation.outcome_saved`
- `recommendation.outcome_deleted`

事件模块限制：

- metadata 只允许小型 JSON；
- 字符串标量最长 300 字符，总 JSON 最长 1000 字符；
- 拒绝密钥样式字段和值；
- `dedup_key` 可用于幂等回填；
- backfill 是显式 API，**启动时不会自动回填历史**。

对 1.3.5 的影响：

- 复盘可复用行为事件，但必须承认事件表上线前的历史不完整；
- 建议跟进提醒优先使用 `recommendation.accepted` 事件，缺失时可退回 `recommendations.reviewed_at/updated_at`，但覆盖率标记为 `partial_data`；
- 提醒自己的展示、延期、取消、完成也应记录最小事件，但 B 批需先扩展事件白名单，不能写入未登记事件类型。

### 1.5 建议、反馈和结果

证据：`app/recommendations.py:41-70`、`app/recommendations.py:284-317`、`app/main.py:527-636`、`app/recommendation_feedback.py`、`app/recommendation_stats.py:390-456`。

建议主状态：

- `pending`
- `accepted`
- `ignored`
- `rejected`
- `expired`

允许转换：

- `pending -> accepted`
- `pending -> ignored`
- `pending -> rejected`
- `accepted -> pending`（reopen，撤销采纳）

建议反馈和结果：

- 每条建议最多一条当前反馈；
- 每条建议最多一条当前结果；
- 结果只允许在建议当前为 `accepted` 且未逻辑过期时创建、更新、删除；
- 撤销采纳后历史结果可读但不可改；
- 无反馈是未知，不是无用；
- 无结果是未观测，不是未执行；
- `causality_status` 固定为 `not_inferred`。

统计服务当前提供：

- 建议当前状态和窗口分布；
- 当前反馈分布；
- 当前采纳数/率；
- 历史生命周期事件数；
- 当前结果数、执行状态分布、完成率；
- 效果评级分布；
- 用户主动登记的可比指标均值差；
- 零分母比例为 `null`；
- 小样本标记 `insufficient_data`；
- 不提供综合因果分。

对 1.3.5 的影响：

- 建议跟进提醒只针对 `accepted` 且未过期建议；
- 一旦存在结果记录（包括 `planned`），第一版即视为“已登记后续状态”，不再催促登记结果；
- 建议 reopen、reject、delete、过期、有结果都必须取消待发送跟进提醒；
- 复盘必须继续区分当前记录与历史事件，不得把相关性表述为因果。

### 1.6 确定性统计和时区

证据：`app/analytics.py:1-96`、`app/analytics.py` 中 `build_window()` / `compare_periods()`；`app/recommendation_stats.py:58-112`、`390-456`。

现有分析模块：

- 不调用模型；
- 使用程序计算窗口和计数；
- 当前支持：
  - `UTC`
  - `Asia/Shanghai`
  - 固定 `UTC±HH:MM`
- 不支持完整 IANA 时区数据库或夏令时转换；
- 日窗口从本地 00:00 开始，半开区间 `[start, end)`；
- 周期对比目前只支持 7/7 或 30/30 等长窗口；
- 小样本阈值为 5；
- 统计只描述数值变化，不解释人格、动机或原因。

对 1.3.5 的影响：

- 1.3.5 第一版设置同样只支持 `UTC`、`Asia/Shanghai`、固定偏移；
- 每日/每周复盘必须使用半开窗口；
- 日期型 `due_date` 按用户设置时区解释；
- 不提前引入完整时区库；
- 若用户更改时区，历史报告保留生成时时区，不重算为同一时期的新报告。

### 1.7 前端现状

证据：`app/static/index.html:14-15`、`95-119`、`127-151`；`app/static/work.js`；`app/static/recommendations.js`。

当前导航已有：

- 总览、AI 助手、长期记忆、私人知识库、数据源；
- 项目与任务、个人画像、行为洞察、智能建议、个人档案、操作审批。

未发现：

- “提醒中心”导航；
- 复盘报告页面；
- 提醒设置表单；
- 提醒或复盘 API 调用；
- 外部通知 token/订阅能力。

现有项目与任务页：

- 可展示任务 `due_date`；
- 前端将当前日期之前且未完成任务标识为“已逾期”；
- 该展示是浏览器本地日期比较，不是服务端提醒规则。

对 1.3.5 的影响：

- E 批应新增独立“提醒/复盘”入口，或在现有导航中新增“提醒中心”；
- 必须沿用纯文本转义、事件委托和现有按钮样式；
- 不应在 A/B/C 批提前改前端；
- 第一批只做应用内提醒，不做浏览器 Push 或手机通知。

### 1.8 测试隔离现状

证据：`tests/test_analytics.py`、`tests/test_behavior_events.py`、`tests/test_connector_schedule.py`、`tests/test_recommendation_*.py` 中的 `isolated_db/conn` fixture。

当前较新的测试普遍使用：

```python
monkeypatch.setenv("DATABASE_PATH", str(db_path))
monkeypatch.setattr("app.main.DB_PATH", db_path)
init_db()
```

1.3.5 必须沿用：

- 每个测试独立临时 SQLite；
- 同时改环境变量和已导入全局 `DB_PATH`；
- 调度测试使用虚拟 clock、可注入 sleep/runner，不真实等待；
- 浏览器冒烟使用临时库和非 8000 端口；
- 核验真实开发库前后长度、mtime、SHA-256。

---

## 2. 已有能力、部分能力与缺失能力

### 2.1 已有能力

| 能力 | 状态 | 可复用方式 |
| --- | --- | --- |
| 任务标题、状态、优先级、日期截止日 | 已有 | 截止提醒和任务状态取消 |
| 目标日期 | 已有 | 第一版可不生成目标提醒，或仅在后续明确规则后启用 |
| 项目事件 | 已有 | 复盘项目活动，不直接推断项目健康度 |
| 行为事件 | 已有 | 复盘统计、建议采纳/结果跟进、幂等历史依据 |
| 建议状态机 | 已有 | 建议跟进提醒的取消条件 |
| 建议结果 | 已有 | 有结果即停止跟进；无结果不代表未执行 |
| 确定性统计 | 已有 | 每日/每周复盘的事实区 |
| 单进程 asyncio 调度样例 | 已有 | 参考连接器调度器，不直接复用业务逻辑 |
| SQLite 外键、唯一约束、条件更新 | 已有 | 去重、级联、重启恢复的基础 |
| Host/Origin 防护 | 已有 | API 层继续复用，但不是登录认证 |

### 2.2 部分能力

| 能力 | 缺口 | 设计处理 |
| --- | --- | --- |
| 任务截止时间 | 只有日期，无具体时间/时区 | 按用户时区的日期规则提醒，不做精确时刻 deadline |
| 任务停滞 | 有 `updated_at` 和事件，但不能证明线下无行动 | 只提示“未观测到记录更新”，低置信度、可关闭 |
| 建议采纳历史 | 行为事件上线前可能缺失 | 用 `reviewed_at/updated_at` 兜底并标记部分覆盖 |
| 重启恢复 | 数据源调度有 next_sync_at；提醒尚无表 | 新提醒表必须有 scheduled_at、状态、唯一 dedup_key 和恢复逻辑 |
| 应用内通知 | 页面可展示列表，但无通知中心 | E 批新增页面；第一版仅 in-app |
| 模型整理复盘 | 有模型调用模式，但默认必须关闭 | D 批先做规则复盘，模型只在服务端显式开启后改写 |

### 2.3 未发现/本阶段不具备

- 未发现 `reminders` 表；
- 未发现 `review_reports` 表；
- 未发现提醒规则引擎；
- 未发现提醒设置（频率、时区、安静时段）存储；
- 未发现提醒调度执行记录；
- 未发现邮件、短信、移动端 Push、Web Push 订阅；
- 未发现完整 IANA 时区/夏令时支持；
- 未发现多用户身份与权限模型；
- 未发现跨进程分布式锁；
- 未发现复盘确认后写入长期记忆的独立授权机制。

---

## 3. 提醒场景与完整规则表

第一版规则均为内置、确定性、白名单规则，不允许客户端提交任意规则表达式。

通用原则：

- 只使用已有结构化数据；
- 每条提醒必须有证据窗口和覆盖率；
- 证据不足不生成；
- 提醒是“可检查的线索”，不是对用户动机、人格或自律的评价；
- 所有自由文本按纯文本展示；
- 所有规则有 `rule_code` 和 `rule_version`；
- 同一场景同一时间槽只能有一条未结束提醒；
- 用户主动延期/忽略优先于系统再次生成；
- 应用关闭期间不堆积多条重复提醒。

### 3.1 规则总表

| 规则代码 | 场景 | 数据来源 | 首次触发 | 检查周期 | 去重键 | 冷却/次数 | 每日上限 | 取消条件 | 过期条件 | 证据不足处理 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `task.due_soon_v1` | 任务即将到期 | `tasks.due_date/status` | 到期日前 1 天本地 09:00 | 5 分钟轮询 | `task.due_soon:{task_id}:{due_date}` | 每个 due_date 1 次 | 3 | done/cancelled、删除、due_date 改期、规则关闭 | due_date 当日 23:59 后转过期或由逾期规则替代 | 无 due_date 不生成 |
| `task.overdue_v1` | 任务已逾日期 | `tasks.due_date/status` | 到期日后本地 09:00 | 5 分钟 | `task.overdue:{task_id}:{due_date}` | 首次后每 3 天最多 1 次，最多 2 次 | 2 | done/cancelled、删除、改期 | due_date 后 7 天 | 无 due_date/已完成不生成 |
| `task.stalled_doing_v1` | 进行中任务长期无记录更新 | `tasks.status/updated_at`、行为事件 | `doing` 且 3 天无记录更新，本地 10:00 | 5 分钟 | `task.stalled:{task_id}:{stalled_week}` | 每 7 天 1 次，最多 2 次 | 2 | done/cancelled、删除、离开 doing、有关联新事件 | 创建 30 天后 | 非 doing、无状态时间、样本不足不生成 |
| `recommendation.followup_v1` | 已采纳建议长期未登记结果 | recommendations、outcomes、behavior_events | accepted 后 7 天本地 19:00 | 5 分钟 | `rec.followup:{rec_id}:{occurrence}` | 第 7、14 天各 1 次，最多 2 次 | 2 | 有结果、reopen/reject/delete、过期、反馈偏好暂停、规则关闭 | 建议过期后 7 天 | 采纳时间不可靠且无 reviewed_at 不生成 |
| `review.daily_due_v1` | 每日复盘 | 设置、统计窗口 | 用户设置时间，默认 20:00，默认关闭 | 5 分钟 | `review.daily:{period_start}` | 每个自然日 1 条 | 1 | 关闭每日复盘、报告已确认/已存在 | 次日本地 02:00 | 统计无数据仍可生成空数据草稿，但标记 no_data |
| `review.weekly_due_v1` | 每周复盘 | 设置、统计窗口 | 周一 09:00，默认关闭 | 5 分钟 | `review.weekly:{period_start}` | 每个 ISO 周 1 条 | 1 | 关闭每周复盘、报告已确认/已存在 | 计划时间后 24 小时 | 空数据可生成 no_data 草稿 |

### 3.2 截止日期规则的日期语义

由于任务和目标只有 `YYYY-MM-DD`：

- `due_date=2026-10-20` 在用户设置时区表示该本地日期；
- “即将到期”在 `2026-10-19 09:00` 本地时间生成；
- “已逾日期”从 `2026-10-21 09:00` 本地时间生成，而不是在 `2026-10-20 00:00` 立刻判定逾期；
- `2026-10-20` 当天称为“今日截止”，不称为已逾期；
- 不保存或展示具体时分秒截止承诺。

若任务改期：

1. 服务层在同一事务取消该任务所有未结束的 due/overdue 提醒；
2. 根据新日期重新调度；
3. 取消原因记为 `source_rescheduled`；
4. 已结束的历史提醒保留。

### 3.3 停滞规则的证据边界

第一版只处理 `status='doing'` 的任务。

系统停滞时间取以下两者较晚者：

- `tasks.updated_at`；
- 该任务在 `behavior_events` 中最新的 `task.updated/task.reopened` 事件时间。

任务创建时间作为下限，不把未来的事件纳入。

提醒文案必须使用：

> 系统最近 3 天未观测到该任务的记录更新。你可能已在线下推进，可选择记录进展、延期或忽略。

禁止使用：

- “你拖延了”；
- “你没有执行”；
- “你缺乏自律”；
- “项目已经停滞”（仅凭单任务不能推断项目）。

### 3.4 建议跟进规则

纳入条件：

- 建议当前为 `accepted`；
- 建议未逻辑过期；
- 无当前 outcome；
- 有可靠 accepted 时间：
  - 优先 `recommendation.accepted` 事件；
  - 次选 `reviewed_at/updated_at`，证据覆盖率标记 `partial_data`；
- 未被用户对同类建议设置临时暂停且仍在暂停期。

停止条件：

- 新增任意 outcome（包括 `planned`、`completed`、`no_longer_relevant`）；
- 建议 reopen、reject、delete 或过期；
- 到达最大提醒次数；
- 用户 dismiss 当前来源提醒。

文案必须说明：

> 没有结果记录只表示系统尚未观测到登记结果，不代表你没有执行。

### 3.5 错过计划时间后的策略

| 场景 | 应用离线/重启后 | 策略 |
| --- | --- | --- |
| 每日复盘 | 错过 ≤2 小时 | 补发 1 条，标记 `catchup=true` |
| 每日复盘 | 错过 >2 小时或已跨到下一计划时间 | 跳过，不堆积 |
| 每周复盘 | 错过 ≤24 小时 | 补发 1 条，标记 `catchup=true` |
| 每周复盘 | 错过 >24 小时 | 跳过 |
| 任务到期 | due_date 后仍在有效窗口 | 启动后最多补 1 条，不按轮询次数堆积 |
| 停滞提醒 | 错过多个检查点 | 只生成当前仍满足条件的 1 条 |
| 建议跟进 | 错过第 7 天但第 14 天未到 | 只补当前 occurrence，不补历史多次 |

### 3.6 安静时段

设置存储本地时间：

- `quiet_hours_enabled`
- `quiet_start`: `HH:MM`
- `quiet_end`: `HH:MM`

规则：

- 提醒到点但处于安静时段：不标记失败，顺延到安静时段结束后的第一个调度 tick；
- 若安静时段跨午夜，按本地时间区间判断；
- 若顺延后超过该提醒过期时间，则直接过期；
- 每日/每周复盘也遵守安静时段；
- 任务已经逾期不会绕过安静时段弹通知，但会在提醒中心保留为待查看项。

第一版没有系统级弹窗，只有应用内列表；安静时段主要为未来通知和列表“新提醒”标记做一致准备。

---

## 4. 提醒状态机（设计，本批不实现）

### 4.1 状态定义

| 状态 | 含义 |
| --- | --- |
| `scheduled` | 已创建，计划时间在未来 |
| `due` | 已到可展示时间，应用内提醒中心可见为待处理 |
| `snoozed` | 用户主动延期，等待 `snooze_until` |
| `dismissed` | 用户明确忽略/不再提醒此 occurrence |
| `completed` | 关联动作已完成或复盘报告已处理 |
| `cancelled` | 系统因来源状态变化、设置关闭、改期等取消 |
| `expired` | 超过有效期，不再展示或调度 |
| `failed` | 调度/生成过程临时失败，可有限重试 |

### 4.2 状态转换

```text
scheduled -> due
scheduled -> cancelled
scheduled -> expired

due -> snoozed
snoozed -> due

due -> dismissed
due -> completed
due -> cancelled
due -> expired

snoozed -> dismissed
snoozed -> cancelled
snoozed -> expired

failed -> due
failed -> expired
```

非法转换必须由服务层拒绝并返回 409；API 不能直接写任意状态字符串。

### 4.3 终态

终态：

- `dismissed`
- `completed`
- `cancelled`
- `expired`

终态记录不可修改。若来源再次满足条件，应通过新的 occurrence / dedup key 创建新提醒，而不是复活旧提醒。

### 4.4 系统取消原因码

- `source_completed`
- `source_cancelled`
- `source_deleted`
- `source_rescheduled`
- `recommendation_closed`
- `outcome_registered`
- `rule_disabled`
- `settings_disabled`
- `superseded_by_new_schedule`
- `expiry_reached`

取消原因码只记录机器枚举和必要 ID，不保存敏感正文。

---

## 5. 每日/每周复盘设计

### 5.1 周期窗口

所有窗口均为半开区间 `[start, end)`。

#### 每日复盘

- 统计上一个本地自然日；
- 例如 2026-10-12 20:00 生成的日报：
  - `2026-10-11 00:00 Asia/Shanghai <= t < 2026-10-12 00:00 Asia/Shanghai`；
- 存储 UTC 边界，同时保存生成时 timezone 和本地时间。

#### 每周复盘

- 默认周一 09:00 生成；
- 统计上一个完整 ISO 周；
- 以上海时区周一 00:00 为界：
  - `上周一 00:00 <= t < 本周一 00:00`；
- 第一版周一为每周复盘日，后续可允许用户选择周日/周一/周五。

### 5.2 数据来源与内容分区

报告内容必须分区标记来源：

| 分区 | 来源 | 来源标签 | 可否由模型改写 |
| --- | --- | --- | --- |
| 任务创建/完成/取消/重开 | tasks + behavior_events | `system_statistic` | 可整理，不可改数 |
| 项目活动 | project_events + behavior_events | `system_statistic` | 可总结，不可推断原因 |
| 目标变化 | goals / behavior_events | `system_statistic` | 可列事实 |
| 建议处理 | recommendation 生命周期事件 | `system_statistic` | 可汇总 |
| 建议反馈 | recommendation_feedback 当前记录 | `user_feedback_current` | 可摘要，不视为历史版本 |
| 建议结果 | recommendation_outcomes 当前记录 | `user_reported_outcome` | 可展示，不推断因果 |
| 数据源同步 | sync_logs/data_sources | `system_statistic` | 可列成功/失败次数 |
| 用户收获/困难/补充 | 复盘表单 | `user_statement` | 用户确认后保存 |
| AI 候选总结 | 规则证据 + 模型输出 | `ai_candidate_unconfirmed` | 默认关闭，需用户确认 |

### 5.3 每日复盘最小内容

- 窗口和时区；
- 数据覆盖率；
- 任务创建、完成、取消、重开数量；
- 截止日变化（仅统计，不评价）；
- 项目事件数量与类型；
- 建议接受/忽略/拒绝/撤销事件；
- 已登记的结果和评级分布；
- 数据源同步尝试、成功、失败；
- “无记录/无结果”的缺失说明；
- 用户可填写：
  - 今日收获；
  - 困难/阻塞；
  - 明日一个最小行动（仅作为复盘文本，不自动创建任务）。

### 5.4 每周复盘最小内容

- 本周窗口与上周等长窗口对比；
- 任务和项目活动总数；
- 任务完成率：分母为当周有完成机会的任务事件，需在实现时固定公式；
- 建议反馈/结果当前分布；
- 已确认目标与里程碑日期；
- 可比指标观察（继续使用同 metric + unit 规则）；
- 数据覆盖不足的类别；
- 用户可填写：
  - 本周收获；
  - 未完成/延后事项；
  - 风险与阻碍；
  - 下周重点；
  - 是否需要把某条用户陈述写入长期记忆（默认不写，只展示未来可选动作）。

### 5.5 数据不足降级

- 空库或窗口内无事件：仍可生成报告草稿，但状态和覆盖状态为 `no_data`；
- 样本 <5：显示“样本不足”，不输出强趋势；
- 只有当前反馈/结果，没有历史版本：明确显示“当前记录”；
- 建议结果缺失：显示“未登记结果”，不显示“未完成”；
- 指标单位不同：不聚合；
- 统计异常：生成 `failed` 草稿并保存脱敏错误，允许重试。

### 5.6 模型边界

默认：

- `REVIEWS_USE_MODEL` 未设置、空或非 `1`：不调用模型；
- 即使设置为 `1`，也必须存在服务端模型 API Key；
- 客户端不能通过请求参数强制启用模型。

模型只能：

- 将程序给定的统计和证据组织成更易读的中文；
- 提出待确认的复盘候选要点；
- 标记为 `ai_candidate_unconfirmed`。

模型不能：

- 创造数字、ID、事件或来源；
- 改变统计值；
- 推断因果关系；
- 判断人格、动机、稳定性习惯；
- 写入任务、目标、记忆、画像；
- 执行外部操作；
- 看到密钥、完整文档内容或不必要的自由文本。

模型失败时：

- 使用规则模板生成报告；
- 状态可为 `ready_rule_only` 或在 `generation_meta_json` 中记录 `model_status=disabled/failed/degraded`；
- 不保存未经验证的模型输出。

### 5.7 报告生命周期

建议状态：

| 状态 | 含义 |
| --- | --- |
| `generating` | 已领取生成任务，防止并发生成 |
| `draft` | 系统生成，尚未由用户确认 |
| `confirmed` | 用户查看/编辑并确认 |
| `archived` | 用户归档，默认列表不展示 |
| `failed` | 生成失败，可重试 |

转换：

```text
generating -> draft
generating -> failed
failed -> generating -> draft
draft -> confirmed
confirmed -> archived
archived -> confirmed（第一版可允许恢复）
```

重新生成：

- 只允许在 `draft/failed` 状态；
- 保留同一报告 ID；
- 旧的 AI 候选不进入长期记忆；
- 可记录 `regeneration_count`，但第一版不建历史版本表。

用户确认：

- 只表示报告已读/认可；
- 不自动创建任务；
- 不自动修改目标；
- 不自动写入 memories/profile_items；
- 若未来要写入长期记忆，必须另做明确勾选和独立授权。

---

## 6. 候选数据模型与索引（供 B/D 批实现，A 批不建表）

### 6.1 表清单建议

推荐最小新增 4 张表：

1. `reminder_settings`：单用户提醒与复盘设置；
2. `reminders`：具体提醒 occurrence；
3. `reminder_scheduler_runs`：调度 tick/恢复/失败记录；
4. `review_reports`：每日/每周复盘报告。

不建议第一版新增提醒历史版本表、报告历史版本表或外部通知投递表。若未来做多设备推送，再新增 `reminder_deliveries`。

### 6.2 `reminder_settings`

单例行，固定 `id=1`。

| 字段 | 类型 | 约束/默认 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PK CHECK(id=1) | 单用户本地版本 |
| `timezone` | TEXT | NOT NULL DEFAULT 'Asia/Shanghai' | UTC/Asia/Shanghai/UTC±HH:MM |
| `task_due_enabled` | INTEGER | NOT NULL DEFAULT 0 | 显式开启后才调度 |
| `task_stalled_enabled` | INTEGER | NOT NULL DEFAULT 0 | 默认关闭，避免打扰 |
| `recommendation_followup_enabled` | INTEGER | NOT NULL DEFAULT 1 | 可在页面关闭 |
| `daily_review_enabled` | INTEGER | NOT NULL DEFAULT 0 | 默认关闭 |
| `weekly_review_enabled` | INTEGER | NOT NULL DEFAULT 0 | 默认关闭 |
| `daily_review_at` | TEXT | NOT NULL DEFAULT '20:00' | 本地 HH:MM |
| `weekly_review_weekday` | INTEGER | NOT NULL DEFAULT 1 | ISO 周一=1 |
| `weekly_review_at` | TEXT | NOT NULL DEFAULT '09:00' | 本地 HH:MM |
| `quiet_hours_enabled` | INTEGER | NOT NULL DEFAULT 0 | 安静时段开关 |
| `quiet_start` | TEXT | NOT NULL DEFAULT '22:00' | 本地 HH:MM |
| `quiet_end` | TEXT | NOT NULL DEFAULT '08:00' | 本地 HH:MM |
| `rule_config_json` | TEXT | NOT NULL DEFAULT '{}' | 阈值、提前量、次数上限 |
| `created_at` | TEXT | NOT NULL | UTC ISO seconds |
| `updated_at` | TEXT | NOT NULL | UTC ISO seconds |

`rule_config_json` 白名单示例：

```json
{
  "task_due_lead_days": 1,
  "task_due_hour": 9,
  "overdue_repeat_days": 3,
  "overdue_max_occurrences": 2,
  "stalled_doing_after_days": 3,
  "stalled_repeat_days": 7,
  "stalled_max_occurrences": 2,
  "recommendation_followup_days": [7, 14]
}
```

不允许把任意 SQL、cron 表达式或提示词存入配置。

### 6.3 `reminders`

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `rule_code` | TEXT NOT NULL | 白名单规则 |
| `rule_version` | TEXT NOT NULL | 如 `1.3.5-B@1` |
| `source_type` | TEXT NOT NULL | `task/recommendation/review` |
| `source_id` | INTEGER | 可选；删除源后的处理依赖外键/服务取消 |
| `task_id` | INTEGER REFERENCES tasks(id) ON DELETE CASCADE | 任务类提醒 |
| `recommendation_id` | INTEGER REFERENCES recommendations(id) ON DELETE CASCADE | 建议类提醒 |
| `review_report_id` | INTEGER | 可延后到 D 批加 FK 或不加 FK |
| `title` | TEXT NOT NULL | 纯文本，长度限制 |
| `body` | TEXT NOT NULL | 纯文本，长度限制 |
| `priority` | TEXT NOT NULL DEFAULT 'normal' | `low/normal/high` |
| `status` | TEXT NOT NULL DEFAULT 'scheduled' | 状态机枚举 |
| `evidence_json` | TEXT NOT NULL DEFAULT '[]' | 来源、窗口、实际值、覆盖率 |
| `coverage_status` | TEXT NOT NULL DEFAULT 'no_data' | `ok/partial_data/insufficient_data/no_data` |
| `dedup_key` | TEXT NOT NULL UNIQUE | 数据库兜底幂等 |
| `scheduled_at` | TEXT NOT NULL | UTC，计划进入 due 的时间 |
| `snoozed_until` | TEXT | 延期后的 UTC 时间 |
| `expires_at` | TEXT NOT NULL | UTC 过期时间 |
| `due_at` | TEXT | 实际进入 due 的时间 |
| `resolved_at` | TEXT | completed/dismissed/cancelled/expired 时间 |
| `cancel_reason` | TEXT | 取消原因枚举 |
| `occurrence_index` | INTEGER NOT NULL DEFAULT 1 | 同类第几次提醒 |
| `attempt_count` | INTEGER NOT NULL DEFAULT 0 | 调度尝试次数 |
| `max_attempts` | INTEGER NOT NULL DEFAULT 3 | 失败重试上限 |
| `last_error` | TEXT NOT NULL DEFAULT '' | 脱敏后的短错误 |
| `created_at` / `updated_at` | TEXT NOT NULL | |

索引建议：

- `UNIQUE(dedup_key)`；
- `idx_reminders_status_scheduled ON reminders(status, scheduled_at)`；
- `idx_reminders_source ON reminders(source_type, source_id)`；
- `idx_reminders_task ON reminders(task_id)`；
- `idx_reminders_rec ON reminders(recommendation_id)`；
- 可选部分索引：`WHERE status IN ('scheduled','due','snoozed')`，用于活跃提醒扫描。

证据 JSON 中每条证据至少包含：

```json
{
  "kind": "metric|task|recommendation|review_window",
  "label": "任务截止日期",
  "value": "2026-10-20",
  "source_type": "tasks.due_date",
  "source_id": 12,
  "window": {"timezone": "Asia/Shanghai", "start": "...", "end": "..."},
  "coverage_status": "ok",
  "definition_version": "task.due_soon_v1@1"
}
```

### 6.4 `reminder_scheduler_runs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `owner_token` | TEXT NOT NULL DEFAULT '' | 单进程实例标识，不作为安全边界 |
| `status` | TEXT NOT NULL | `running/success/partial/failed` |
| `tick_due_at` | TEXT NOT NULL | 本次 tick 逻辑时间 |
| `started_at` | TEXT NOT NULL | |
| `heartbeat_at` | TEXT | |
| `finished_at` | TEXT | |
| `scanned_count` | INTEGER NOT NULL DEFAULT 0 | |
| `due_count` | INTEGER NOT NULL DEFAULT 0 | |
| `created_count` | INTEGER NOT NULL DEFAULT 0 | |
| `due_marked_count` | INTEGER NOT NULL DEFAULT 0 | |
| `snoozed_count` | INTEGER NOT NULL DEFAULT 0 | |
| `expired_count` | INTEGER NOT NULL DEFAULT 0 | |
| `cancelled_count` | INTEGER NOT NULL DEFAULT 0 | |
| `suppressed_count` | INTEGER NOT NULL DEFAULT 0 | |
| `failed_count` | INTEGER NOT NULL DEFAULT 0 | |
| `error` | TEXT NOT NULL DEFAULT '' | 脱敏、截断 |

恢复规则：

- startup 将运行超过 5 分钟且仍为 `running` 的记录标记为 `failed`；
- 是否重复以 `reminders.dedup_key` 和状态条件更新为准；
- `owner_token` 仅用于诊断，不作为锁；
- 不宣称多进程下运行记录完全串行。

### 6.5 `review_reports`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `period_type` | TEXT NOT NULL | `daily/weekly` |
| `period_start` | TEXT NOT NULL | UTC ISO |
| `period_end` | TEXT NOT NULL | UTC ISO，半开区间 |
| `timezone` | TEXT NOT NULL | 生成时时区 |
| `status` | TEXT NOT NULL | `generating/draft/confirmed/archived/failed` |
| `definition_version` | TEXT NOT NULL | 复盘口径版本 |
| `source` | TEXT NOT NULL DEFAULT 'rule' | `rule/hybrid` |
| `stats_snapshot_json` | TEXT NOT NULL DEFAULT '{}' | 程序统计快照 |
| `sections_json` | TEXT NOT NULL DEFAULT '[]' | 分区内容和证据引用 |
| `evidence_refs_json` | TEXT NOT NULL DEFAULT '[]' | 表名、ID、窗口、来源标签 |
| `limitations_json` | TEXT NOT NULL DEFAULT '[]' | 数据限制 |
| `user_wins_note` | TEXT NOT NULL DEFAULT '' | 用户填写收获 |
| `user_blockers_note` | TEXT NOT NULL DEFAULT '' | 用户填写困难 |
| `user_extra_note` | TEXT NOT NULL DEFAULT '' | 用户补充 |
| `next_step_note` | TEXT NOT NULL DEFAULT '' | 用户写下的下一步，不自动建任务 |
| `generation_meta_json` | TEXT NOT NULL DEFAULT '{}' | 模型状态、重试、耗时，不存 prompt/key |
| `error` | TEXT NOT NULL DEFAULT '' | 脱敏错误 |
| `regeneration_count` | INTEGER NOT NULL DEFAULT 0 | |
| `generated_at` | TEXT | |
| `confirmed_at` | TEXT | |
| `archived_at` | TEXT | |
| `created_at` / `updated_at` | TEXT NOT NULL | |

唯一键：

```sql
UNIQUE(period_type, period_start, period_end)
```

同一周期重复点击生成：

- draft/failed：重新生成同一行；
- confirmed/archived：默认不重新生成，返回 409；
- 不允许同一周期出现多个报告。

### 6.6 是否需要行为事件新类型

B/C 批如要记录提醒生命周期，建议在 `app/behavior_events.py` 扩展白名单：

- `reminder.created`
- `reminder.shown`
- `reminder.snoozed`
- `reminder.dismissed`
- `reminder.completed`
- `reminder.cancelled`
- `reminder.expired`
- `review_report.generated`
- `review_report.confirmed`
- `review_report.archived`

若希望 B 批更小，第一版也可先只使用提醒表自身时间戳，不写 behavior_events；待 E/F 批再补审计事件。**建议 B 批先不扩展事件，C 批调度成型后再统一加事件，避免事件类型先行但语义反复修改。**

---

## 7. 调度、时区、幂等与恢复方案

### 7.1 推荐调度形态

新增独立模块，不修改连接器调度器语义：

- `app/reminders.py`：规则、存储、状态机、纯时间计算；
- `app/reminder_scheduler.py`：轮询和事务编排（C 批）；
- `app/review_reports.py`：复盘聚合和草稿生成（D 批）。

轮询建议：

- 默认每 300 秒（5 分钟）一次；
- startup 后延迟 3-5 秒首次 tick；
- 每次 tick 使用独立事务；
- 同进程 `asyncio.Lock` 防重叠；
- 数据库条件更新和唯一键承担幂等。

### 7.2 单次 tick 事务边界

建议步骤：

1. 打开一个 `db()` 事务；
2. 恢复 stale scheduler run；
3. 创建 `reminder_scheduler_runs(status=running)`；
4. 取消已不满足条件的活跃提醒；
5. 将到期提醒从 `scheduled/snoozed` 条件更新为 `due`；
6. 按规则扫描来源并插入新提醒，`INSERT ... SELECT WHERE NOT EXISTS` 或捕获唯一约束；
7. 过期活跃提醒；
8. 更新 run 计数为 success/partial/failed；
9. commit。

部分失败：

- 单条来源评估失败：记录 `failed_count` 和脱敏错误，继续处理其他来源；
- 核心事务失败：回滚，run 标 failed；
- 不在循环中对每条提醒单独 commit，避免失败后产生不可解释的半批状态。

### 7.3 幂等键示例

| 规则 | dedup_key |
| --- | --- |
| 即将到期 | `task.due_soon_v1:{task_id}:{due_date}` |
| 逾期 | `task.overdue_v1:{task_id}:{due_date}:{occurrence}` |
| 停滞 | `task.stalled_doing_v1:{task_id}:{year}-W{iso_week}:{occurrence}` |
| 建议跟进 | `recommendation.followup_v1:{rec_id}:{accepted_date}:{occurrence}` |
| 每日复盘 | `review.daily_due_v1:{period_start_utc}` |
| 每周复盘 | `review.weekly_due_v1:{period_start_utc}` |

仅靠应用层“先查后插”不足以防并发，必须由 `UNIQUE(dedup_key)` 兜底。

### 7.4 多实例限制

当前产品明确单实例、本地、单用户：

- asyncio Lock 只保护单进程；
- SQLite 唯一键可防止重复提醒；
- 条件状态更新可减少重复 due；
- 但不保证两个进程的 scheduler run 记录完全一一对应，也不提供分布式通知投递保证。

因此：

- 文档和 API 响应必须声明“仅支持单实例”；
- F 批测试可以验证重复 tick/重启恢复，不把它描述成分布式锁；
- 若未来支持多设备/多进程，需要引入数据库租约或外部调度，不属于 1.3.5。

### 7.5 时间存储标准

- 内部时间戳：UTC ISO-8601，秒精度；
- 用户日期：保存原始 `YYYY-MM-DD`，调度时结合 settings.timezone；
- 报告保存 UTC 边界和本地时区；
- 所有窗口使用 `[start,end)`；
- 比较字符串时间时必须统一 UTC ISO 格式；
- 第一版不处理夏令时；
- 固定 UTC offset 与 Asia/Shanghai 的规则要分别测试。

---

## 8. 隐私、安全与产品边界

### 8.1 不自动行动

提醒和复盘不得：

- 自动创建/修改任务；
- 自动改变项目或目标状态；
- 自动采纳、忽略、拒绝建议；
- 自动登记建议结果；
- 自动写入 `memories`；
- 自动写入 `profile_items`；
- 自动发送外部消息；
- 自动执行文件、命令或连接器操作。

“下一步”文本第一版只保存在复盘用户备注中；若未来要创建任务，必须进入现有审批/人工确认链路。

### 8.2 证据和文案

- 所有提醒必须展示证据来源、时间窗口、覆盖率；
- “没有记录”只能表示系统未观测到；
- 不根据单个弱信号推断人格、动机、稳定习惯；
- 不把统计相关表述成因果；
- 模型输出必须通过结构、长度、枚举、证据 ID、字段白名单校验。

### 8.3 敏感信息

- 复用现有 secret 检测；
- scheduler run 的错误必须截断、脱敏；
- 不保存 API Key、完整模型 prompt、文档全文；
- behavior event metadata 继续受键名、字符串长度和 JSON 大小限制；
- 列表 API 不返回不必要的长正文或内部堆栈。

### 8.4 API 与前端安全（E 批）

- GET 必须只读；
- 状态变更使用 POST/PATCH/DELETE；
- 复用现有 Host loopback 和同源状态变更防护；
- 本地无登录模型不变，不能宣称多用户隔离；
- 前端所有标题、正文、错误、用户输入用 `esc()` 或 `textContent`；
- 不增加 inline event handler；
- 操作按钮需要防重复提交；
- 409 冲突不能显示为成功。

---

## 9. B～F 分批实施计划

### 9.1 B：提醒领域模型、存储与规则引擎（不启动调度）

建议新增：

- `app/reminders.py`
- `tests/test_reminders_store.py`
- 在 `app/main.py:init_db()` 中新增 `reminder_settings`、`reminders` 表（仅在批准后实现）

范围：

- Pydantic/服务层输入模型；
- 设置读取和更新；
- 规则版本、阈值、纯时间窗口函数；
- 提醒创建的 dedup；
- 状态机；
- snooze/dismiss/complete/cancel/expire；
- 任务和建议来源的条件评估，但由测试显式调用，不自动运行；
- 外键和级联。

前置依赖：A 批规则默认值获得批准。

验收：

- 重复初始化不丢数据；
- 非法枚举、非法时间、超长文本、秘密文本拒绝；
- 同一 dedup key 不重复；
- 非法状态转换 409；
- 任务/建议删除时提醒关系正确；
- 不写 memories/profile_items；
- 不启动后台任务、不新增路由、不改前端。

### 9.2 C：触发条件与定时调度

建议新增：

- `app/reminder_scheduler.py`
- `tests/test_reminder_scheduler.py`
- `reminder_scheduler_runs` 表
- startup/shutdown 接入

范围：

- 轮询、到期标记、取消和过期；
- 错过窗口 catch-up；
- 安静时段；
- 虚拟 clock 测试；
- 崩溃 run 恢复；
- 重复 tick 幂等；
- 默认仅推荐复盘显式开启；任务类提醒默认关闭，建议跟进可默认开启但只在应用内展示。

验收：

- 重启后不重复提醒；
- 应用离线恢复不堆积；
- 单进程重叠 tick 被跳过；
- 部分失败 run 状态和计数一致；
- 不要求解决多进程分布式锁，但必须记录限制；
- 无真实 sleep、无真实外部通知。

### 9.3 D：每日/每周复盘引擎

建议新增：

- `app/review_reports.py`
- `tests/test_review_reports.py`
- `review_reports` 表

范围：

- 确定性日/周窗口；
- 聚合任务、项目、行为事件、建议反馈、结果、同步日志；
- 空数据和小样本；
- 报告唯一周期约束；
- 规则型报告模板；
- 可选模型整理，默认关闭；
- 模型异常降级；
- 不写长期记忆。

验收：

- 统计数字来自程序，不允许模型改写；
- 模型输出必须引用已有证据 ID；
- 无证据不生成事实性结论；
- 周期唯一约束防并发重复；
- failed 可重试；
- draft 重生成不产生重复周期报告。

### 9.4 E：API、提醒中心与复盘页面

建议修改：

- `app/main.py`
- `app/static/index.html`
- 新增 `app/static/reminders.js`
- API/页面测试

建议 API：

- `GET /api/reminders`
- `GET /api/reminders/{id}`
- `POST /api/reminders/scan`（手动受控扫描，可选）
- `POST /api/reminders/{id}/snooze`
- `POST /api/reminders/{id}/dismiss`
- `POST /api/reminders/{id}/complete`
- `GET/PUT /api/reminder-settings`
- `GET /api/review-reports`
- `GET /api/review-reports/{id}`
- `POST /api/review-reports/generate`
- `POST /api/review-reports/{id}/confirm`
- `POST /api/review-reports/{id}/archive`
- `GET /api/reminder-scheduler-runs`（分页，脱敏）

页面：

- 提醒列表、筛选、详情证据；
- 延期、忽略、完成；
- 设置时区、频率、安静时段；
- 每日/每周报告列表和详情；
- 草稿重新生成、编辑用户备注、确认/归档；
- 加载、空状态、错误、409、防重复提交。

验收：

- API 400/404/409 语义正确；
- 页面纯文本安全渲染；
- 筛选和分页不丢状态；
- 浏览器临时库冒烟通过。

### 9.5 F：安全、性能、完整回归与阶段收口

范围：

- Host/Origin、XSS、输入长度、畸形 JSON；
- 外键、级联、唯一约束和事务回滚；
- 重启恢复、重复 tick、跨进程限制说明；
- 大列表分页、索引和 N+1 检查；
- 全量 pytest、compileall、JS check、浏览器冒烟；
- 真实开发库哈希；
- 文档回填和风险清单。

验收：

- P0 安全问题清零（在本地单用户边界内）；
- 不把单进程调度描述成分布式一致；
- 不把无记录描述成无行动；
- 不自动写记忆/画像；
- 完整报告区分已修复、已知限制、未验证项。

---

## 10. B 批开始前需要用户确认的设计决策

以下采用推荐默认值，若用户不另行修改，B 批可按推荐值实现：

| 决策 | 推荐值 | 影响 |
| --- | --- | --- |
| 任务到期提醒默认开关 | 默认关闭，用户在设置中开启 | 避免现有任务立即被提醒打扰 |
| 任务停滞提醒默认开关 | 默认关闭 | 停滞推断敏感，优先保守 |
| 建议跟进提醒默认开关 | B 批批准值：默认关闭，仅在用户显式开启后应用内展示 | A 批曾建议默认开启；1.3.5-B 启动时已明确改为准许默认关闭 |
| 每日复盘 | 默认关闭，默认 20:00 | 用户明确开启后才生成 |
| 每周复盘 | 默认关闭，周一 09:00 | 用户明确开启后才生成 |
| 时区 | 默认 Asia/Shanghai，支持 UTC/固定偏移 | 与现有 analytics 保持一致 |
| 任务 due 时间语义 | 日期，无时分；提前 1 天 09:00，过期后次日 09:00 | 不虚构具体 deadline |
| doing 停滞阈值 | 3 天无记录更新，每 7 天提醒，最多 2 次 | 可在设置中调整但设上下限 |
| 建议跟进 | accepted 后 7、14 天，最多 2 次 | 有 outcome 即停止 |
| 每日错过补发 | 2 小时内补发，之后跳过 | 防止堆积 |
| 每周错过补发 | 24 小时内补发，之后跳过 | 周报比日报价值更长尾 |
| 报告确认后是否写记忆 | 不写 | 未来必须单独授权 |
| 调度实例范围 | 单实例本地应用 | 不引入分布式锁/新基础设施 |

---

## 11. A 批检查记录

本批实际执行了以下只读/低副作用检查：

- 枚举并阅读现有后端模块、静态页面、测试文件；
- 审计 `init_db()` 中的全部相关表结构；
- 审计任务/目标/项目 API 与时间字段；
- 审计行为事件、建议、反馈、结果、统计模块；
- 审计现有连接器调度器和 startup/shutdown；
- 搜索现有 reminder/review/notification/scheduler 相关代码；
- 确认 `docs/plans/phase-5-1.3.5-reminders-review.md` 原本不存在；
- 记录真实开发库哈希基线。

A 批开始前真实开发库：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

A 批设计文档写入并完成只读复核后，再次核验真实开发库，长度、mtime 与 SHA-256 均保持不变：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```


本批没有运行全量 pytest：A 批仅要求安全只读审计，且全量结果已在 1.3.4-E 中以临时库实测完成。A 批没有为形式而重复执行可能触发初始化的测试；B 批开始后必须重新运行专项和全量测试。

---

## 12. A 批结论

当前代码具备实现 1.3.5 的基础：

- 有可靠的任务日期、状态和行为事件；
- 有建议采纳/结果生命周期；
- 有确定性统计和半开时间窗口；
- 有单进程 asyncio 调度样例；
- 有 SQLite 外键、唯一约束、测试隔离和安全渲染经验。

但也有明确边界：

- 任务/目标只有日期，没有具体截止时刻；
- 未发现提醒/复盘表和用户提醒设置；
- 现有调度器只保证单进程，不提供多实例协调；
- 无外部通知通道；
- 无登录和多用户隔离；
- 历史行为事件不完整。

因此后续实现应采用“规则内置、默认保守、显式开启、数据库幂等、只在应用内展示、事实与推测分离”的方案。A 批到此停止，等待人工审阅并明确批准 1.3.5-B。

---

## 13. 1.3.5-B 实施记录（提醒存储、规则引擎与状态机）

> 状态：**B 批已完成并通过本地测试**。本批只实现持久化领域层、纯规则计算、去重与状态机；没有接入定时调度、HTTP API、前端页面、复盘报告生成或外部通知。

### 13.1 实际新增和修改文件

| 文件 | 类型 | 实际作用 |
| --- | --- | --- |
| `app/reminders.py` | 新增 | 提醒设置模型、提醒草稿/证据模型、确定性规则候选、提醒存储、唯一去重、延期与状态机服务 |
| `app/main.py` | 修改 | 仅在 `init_db()` 中幂等新增 `reminder_settings`、`reminders` 两张表及索引，并导入提醒领域模块；未新增路由、未接入调度 |
| `tests/test_reminders_store.py` | 新增 | 设置默认值、单例幂等、输入校验、提醒 CRUD、唯一约束、外键级联、并发创建和事务回滚测试 |
| `tests/test_reminder_rules.py` | 新增 | 任务到期/逾期/停滞、建议跟进、每日/每周复盘候选、时区、ISO 周、半开窗口和规则关闭测试 |
| `tests/test_reminder_state_machine.py` | 新增 | 合法/非法状态转换、终态、延期、过期、幂等操作和并发状态竞争测试 |

### 13.2 已批准并实际采用的默认值

- 默认时区：`Asia/Shanghai`。
- 任务即将到期提醒：默认关闭。
- 任务逾期提醒：随任务到期提醒开关关闭；仅规则启用时可产生候选。
- 任务停滞提醒：默认关闭。
- 建议跟进提醒：**B 批批准为默认关闭**，用户显式开启后才计算候选。
- 每日复盘提醒：默认关闭，默认本地时间 `20:00`。
- 每周复盘提醒：默认关闭，默认周一 `09:00`。
- 安静时段：默认关闭，默认 `22:00–08:00`；启用后开始时间与结束时间不能相同，跨午夜按本地 HH:MM 区间留给 C 批调度解释。
- 任务日期仍是 `YYYY-MM-DD`，不虚构具体截止时刻；规则使用本地日期和固定钟点计算。

### 13.3 实际数据库结构

#### `reminder_settings`

单例设置表，数据库使用 `CHECK (id = 1)` 保证只能存在 ID=1 的配置行；初始化通过 `INSERT OR IGNORE` 幂等写入默认配置。

核心字段：

- `id`；
- `timezone`；
- `task_due_enabled`；
- `task_stalled_enabled`；
- `recommendation_followup_enabled`；
- `daily_review_enabled`；
- `weekly_review_enabled`；
- `daily_review_at`；
- `weekly_review_weekday`；
- `weekly_review_at`；
- `quiet_hours_enabled`、`quiet_start`、`quiet_end`；
- `created_at`、`updated_at`。

服务层校验：

- 仅支持项目现有时区能力：`UTC`、`Asia/Shanghai`、固定 `UTC±HH:MM`；
- 拒绝非法 HH:MM、非法星期、未知字段和启用状态下相同的安静时段起止时间；
- 更新使用事务和固定单例行，不允许调用方覆盖创建时间等内部字段。

#### `reminders`

具体提醒 occurrence 表。核心字段：

- 标识和来源：`id`、`rule_code`、`rule_version`、`source_type`、`source_id`、`task_id`、`recommendation_id`；
- 展示内容：`title`、`content`、`priority`、`evidence_json`、`coverage_status`；
- 状态：`status`、`dedup_key`、`scheduled_at`、`due_at`、`snoozed_until`、`expires_at`；
- 用户/系统处理时间：`acknowledged_at`、`completed_at`、`cancelled_at`、`resolved_at`、`cancel_reason`；
- 调度辅助但本批不启动：`occurrence_index`、`attempt_count`、`max_attempts`、`last_error`；
- `created_at`、`updated_at`。

数据库约束：

- `dedup_key TEXT NOT NULL UNIQUE`，并发创建由数据库唯一约束兜底；
- `task_id` 外键关联 `tasks(id) ON DELETE CASCADE`；
- `recommendation_id` 外键关联 `recommendations(id) ON DELETE CASCADE`；
- CHECK 约束限制规则代码、来源、优先级、状态、覆盖状态、取消原因、长度和 occurrence 范围；
- 来源一致性 CHECK 保证 task/recommendation/review 三类来源与外键列匹配；
- 活跃索引：`(status, scheduled_at)`、`(source_type, source_id)`、任务 ID、建议 ID，以及活跃提醒部分索引。

### 13.4 实际规则行为

所有规则函数均要求显式传入参考时间 `now`，不隐式读取系统时钟。

| 规则 | 行为 |
| --- | --- |
| `task.due_soon_v1` | 开关启用、任务未完成/取消且有 `due_date` 时，在截止日前一天本地 09:00 后成为候选；截止日本地 23:59 过期 |
| `task.overdue_v1` | 截止日后本地 09:00 首次候选；每 3 天一次，最多 2 次，截止日后 7 天过期 |
| `task.stalled_doing_v1` | 仅 `doing` 任务；3 天无可观测更新后首次候选，之后每 7 天一次，最多 2 次；文案明确“不代表无线下行动” |
| `recommendation.followup_v1` | 默认关闭；仅当前 `accepted` 且未过期建议可生成；采纳后第 7、14 天本地 19:00；存在任意 outcome 即停止；无 outcome 不等于未执行 |
| `review.daily_due_v1` | 默认关闭；复盘上一本地自然日 `[start,end)`；默认 20:00，错过 2 小时后不产生候选；本批不生成报告 |
| `review.weekly_due_v1` | 默认关闭；复盘上一完整 ISO 周 `[start,end)`；默认周一 09:00，错过 24 小时后不产生候选；本批不生成报告 |

规则只返回 `RuleCandidate` / `None`，不写数据库、不启动后台任务、不调用模型、不发送通知。

### 13.5 实际状态机

统一状态集合：

```text
scheduled
due
snoozed
acknowledged
dismissed
completed
cancelled
expired
```

允许的服务操作：

- `mark_due`：`scheduled/snoozed -> due`；
- `snooze`：仅 `due -> snoozed`，延期时间必须晚于当前参考时间；
- `acknowledge`：`due -> acknowledged`；
- `dismiss`：`due/snoozed/acknowledged -> dismissed`，重复 dismiss 幂等；
- `complete`：`due/snoozed/acknowledged -> completed`，重复 complete 幂等；
- `cancel`：非终态记录可取消，要求合法取消原因，重复 cancel 幂等；
- `expire`：`scheduled/due/snoozed -> expired`，重复 expire 幂等。

终态：`dismissed`、`completed`、`cancelled`、`expired`。终态不能通过普通状态操作复活；同一业务再次触发时应由新的 occurrence/dedup key 表达。

`due` 只表示已经具备站内展示资格，不表示外部通知已发送，也不表示用户已读。

### 13.6 去重、并发与事务

- 新建提醒先经 Pydantic/服务层校验，再由数据库 `UNIQUE(dedup_key)` 兜底。
- 并发创建相同 dedup key 时，胜出事务插入，其余连接捕获 `IntegrityError` 后读取并返回已有记录；最终只有一条记录。
- 状态更新使用条件 `WHERE id=? AND status=?`，竞争失败返回 409 风格的 `ReminderError(code="conflict", status_code=409)`。
- 测试使用独立临时 SQLite 文件，并同时设置：
  - `DATABASE_PATH`；
  - 已导入模块的 `app.main.DB_PATH`。
- 外键级联在测试连接中启用 `PRAGMA foreign_keys=ON` 后实测通过。

### 13.7 B 批实际验证结果

#### 专项测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py -q
```

结果：

```text
27 passed, 4 warnings in 6.10s
退出码：0
```

覆盖重点：

- 默认设置、单例幂等和合法更新；
- 非法时区、时间、星期、未知字段、相同安静时段；
- 提醒创建、读取、列表、唯一去重、非法枚举、长度、秘密文本、证据校验；
- 任务外键级联删除；
- 多线程并发相同 dedup key；
- 事务失败回滚；
- 到期、逾期、停滞、建议跟进、日报、周报和跨 ISO 周边界；
- 全部规则关闭时不产生候选；
- 合法/非法状态转换、终态不可复活、snooze、过期、幂等和并发状态竞争。

#### 全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
346 passed, 1 skipped, 5 warnings in 150.53s (0:02:30)
退出码：0
```

#### Python 编译检查

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
退出码：0
```

### 13.8 真实开发数据库核验

B 批收尾前重新读取：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

全量测试与编译检查后再次读取：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

结论：长度、mtime 与 SHA-256 均一致，未发现本批测试或验证修改真实开发数据库。

### 13.9 B 批范围确认与未实现内容

本批未实现：

- `app/reminder_scheduler.py`；
- startup/shutdown 调度接入；
- 后台线程、asyncio task、轮询 tick 或 catch-up 调度；
- 新 HTTP API；
- 前端页面或静态资源修改；
- `review_reports` 表和每日/每周复盘报告生成；
- 外部通知、邮件、短信、Web Push；
- 真实 LLM 调用；
- 自动创建/修改任务、目标、记忆或画像；
- Git 初始化、提交、推送或部署。

### 13.10 留给 1.3.5-C 的前置事项

C 批开始前应围绕已实现领域层完成：

1. 新增 `reminder_scheduler_runs` 的设计与表结构。
2. 定义 tick 的事务边界、失败计数、部分失败计数和崩溃恢复语义。
3. 将 `mark_due`、`expire_reminders`、来源失效取消等服务函数接入受控调度，但仍保持单实例限制。
4. 实现安静时段跨午夜的调度顺延逻辑，并补虚拟 clock 测试。
5. 明确复盘提醒到点后是否只生成提醒，还是在 D 批报告引擎完成前保持“无报告可打开”的站内状态。
6. 继续使用临时数据库，并在 C 批全量测试前后核验真实开发库哈希。

**B 批到此停止，等待人工明确批准后才能开始 1.3.5-C。**
---

## 14. 1.3.5-C-B：提醒调度器核心实现记录（2026-10-13）

> **批次：1.3.5-C-B（调度器存储、运行记录与显式 tick；不接 API、不接 startup、不做前端）**
>
> **状态：实现与自动化验证已完成，等待人工审核。**

### 14.1 本批新增内容

新增文件：

- `app/reminder_scheduler.py`
  - 显式、同步、可测试的 `run_tick(conn, *, now, trigger_type="manual", run_key=None, owner_token="")`；
  - 调度运行记录创建、完成、失败和遗留 `running` 恢复；
  - 任务、建议和复盘候选生成；
  - 候选提醒数据库去重；
  - 过期处理与 `scheduled/snoozed -> due` 推进；
  - 安静时段顺延；
  - 来源完成、取消、改期、建议关闭和已登记 outcome 后的取消；
  - 每日 2 小时、每周 24 小时 catch-up；
  - 同进程模块级锁，只防止同 Python 进程内重叠，不提供跨进程锁。
- `tests/test_reminder_scheduler.py`
- `tests/test_reminder_scheduler_recovery.py`
- `tests/test_reminder_quiet_hours.py`

修改文件：

- `app/main.py`
  - 在 `init_db()` 中幂等新增 `reminder_scheduler_runs` 表及索引；
  - 未新增 reminder HTTP 路由；
  - 未修改 startup/shutdown 生命周期，提醒调度器不会随应用自动启动。

### 14.2 `reminder_scheduler_runs` 表

用途：记录一次显式提醒 tick 的运行状态与脱敏结果，不承担分布式锁职责。

主要字段：

- `id`
- `run_key TEXT NOT NULL UNIQUE`
- `trigger_type`：`manual`、`scheduled`、`recovery`
- `status`：`running`、`succeeded`、`partial_failed`、`failed`、`abandoned`
- `started_at`、`finished_at`、`created_at`
- `owner_token TEXT NOT NULL DEFAULT ''`
- 计数：
  - `processed_count`
  - `created_count`
  - `updated_count`
  - `skipped_count`
  - `due_count`
  - `expired_count`
  - `cancelled_count`
  - `recovered_run_count`
  - `error_count`
- `last_error TEXT NOT NULL DEFAULT ''`

约束和索引：

- `run_key UNIQUE`；
- 触发类型和运行状态 CHECK 约束；
- 所有计数非负；
- `run_key`、`owner_token`、`last_error` 长度限制；
- `(status, started_at)` 与 `(started_at)` 索引。

`owner_token` 只作为诊断信息，不是锁，不是认证或安全边界。

### 14.3 tick 顺序

单次 `run_tick(now=...)` 的顺序：

1. 校验带时区的 `now`、触发类型和 `run_key`。
2. 获取同进程模块级锁。
3. 恢复超过 5 分钟仍为 `running` 的历史运行，标记为 `abandoned`。
4. 使用唯一 `run_key` 创建本次运行记录；重复 key 返回已有运行。
5. 读取提醒设置。
6. 先处理已过期提醒，避免过期记录闪现为 `due`。
7. 检查并处理任务、建议来源失效或规则关闭。
8. 调用 B 批纯规则函数生成候选，并由 `reminders.dedup_key UNIQUE` 去重。
9. 再次处理候选生成后的过期情况。
10. 推进未过期且已到展示时间的 `scheduled/snoozed` 提醒为 `due`。
11. 根据错误数量将运行记录标记为 `succeeded` 或 `partial_failed`；关键失败标记为 `failed`。

### 14.4 安静时段与 catch-up

- `quiet_until(local_now, settings)` 支持：
  - 未启用；
  - 同日 `[start, end)`；
  - 跨午夜 `[start, 24:00) ∪ [00:00, end)`。
- 安静时段内已到点提醒保持原状态，不计为已通知或已读；下一次 tick 再判断。
- 若顺延结束时间不早于提醒过期时间，则将提醒标记为 `expired`，不会无限延长 `expires_at`。
- 每日复盘仍只在上一本地自然日计划时间后 2 小时窗口内生成；周报为上一完整 ISO 周计划时间后 24 小时窗口内生成。
- 错过 catch-up 窗口不补历史积压。

### 14.5 来源失效语义

- 任务完成：活跃任务提醒按 `source_completed` 取消。
- 任务取消：活跃任务提醒按 `source_cancelled` 取消。
- 任务 `due_date` 改期：旧 due-soon/overdue 提醒按 `source_rescheduled` 取消；停滞提醒不因此取消。
- 建议已登记 outcome：对应跟进提醒按 `outcome_registered` 取消。
- 建议撤销、关闭或逻辑过期：对应跟进提醒按 `recommendation_closed` 取消。
- 规则或设置关闭：
  - 停止生成新提醒；
  - 取消尚未展示的 `scheduled/snoozed`；
  - 保留已经 `due` 的历史可见项，不自动清除。
- 任务或建议物理删除仍由既有外键级联删除处理；这种情况不会在运行记录中虚计为 cancelled。

### 14.6 事务、幂等与边界

- 候选和来源处理使用 SQLite SAVEPOINT 隔离单来源错误；一个来源失败可产生 `partial_failed`，不拖垮其他来源。
- `run_key UNIQUE` 防止同一运行 key 重复建运行记录。
- 提醒业务去重仍由 `reminders.dedup_key UNIQUE` 兜底。
- 状态推进复用 B 批状态机，不直接绕过服务层任意改状态。
- 同进程锁不能保护多进程；多进程依赖数据库唯一约束和条件更新降低重复写入，不承诺分布式 exactly-once。
- 本模块没有 HTTP API、前端、startup/shutdown、后台线程、asyncio 常驻循环、外部通知或 LLM 调用。

### 14.7 C-B 自动化验证结果

#### C 批新增专项测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py -q
```

最终结果：

```text
24 passed, 4 warnings
退出码：0
```

覆盖重点：

- 空库 tick；
- 重复 `run_key`；
- 候选创建、重复 tick 去重；
- `scheduled -> due`、`snoozed -> due`；
- 过期优先于 due；
- 安静时段普通区间、跨午夜、半开边界；
- 安静顺延跨过期时间后标记 expired；
- 每日 2 小时与每周 24 小时 catch-up 边界；
- 任务完成、改期取消旧提醒；
- 建议跟进、outcome 登记和规则关闭；
- 遗留 `running` 5 分钟恢复；
- 部分失败；
- 独立 SQLite 连接和唯一 run key 行为。

#### 提醒相关完整专项测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py -q
```

结果：

```text
50 passed, 4 warnings
退出码：0
```

#### 全量测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

最终结果：

```text
370 passed, 1 skipped, 5 warnings in 95.51s (0:01:35)
退出码：0
```

#### Python 编译检查

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
退出码：0
```

本批未修改前端文件，项目根目录未发现 `package.json`，因此没有可执行的 JavaScript npm 检查。

### 14.8 真实开发数据库核验

C-B 开始前读取：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

C-B 全量测试与编译检查后再次读取：

```text
data/personal_ai_os.db
Length: 258048
mtime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

结论：大小、修改时间和 SHA-256 均一致，未发现测试或验证修改真实开发数据库。

### 14.9 仍未实现的 C 批后续内容

以下内容不属于 C-B，未标记为完成：

- HTTP API；
- 提醒中心前端；
- FastAPI startup/shutdown 自动调度；
- 后台常驻轮询；
- 外部通知；
- 每日/每周复盘报告生成；
- 真实 LLM 调用；
- 自动创建或修改任务、目标、记忆和画像；
- Git 初始化、提交、推送或部署。

**1.3.5-C-B 到此停止，等待人工审核；未自动进入下一批。**

---

## 15. 1.3.5-C-B-R：调度器可靠性修复与回归（2026-10-13）

### 15.1 修复范围

本批仅根据 C-B 人工只读审查修复调度器核心可靠性问题，不新增 API、不修改前端、不接入 startup/shutdown、不启动后台调度、不调用 LLM、不发送外部通知、不生成复盘正文、不修改任务/目标/记忆/画像。

### 15.2 P1：任务改期识别

- `app/reminder_scheduler.py::_task_reminder_is_stale()` 现在优先读取 `reminders.evidence_json` 中结构化的“任务截止日期”快照。
- 结构化快照必须同时匹配：提醒规则为 `task.due_soon_v1` / `task.overdue_v1`、证据 `kind=task`、`label=任务截止日期`、`source_type=tasks`、`source_id` 等于当前提醒来源 ID，且值符合 `YYYY-MM-DD`。
- 历史记录缺少结构化快照时，才回退解析既有 dedup key。
- dedup key 不符合历史格式、日期不合法或无法可靠识别时，采取保守策略：不误取消提醒。
- `task.stalled_doing_v1` 不参与截止日期改期取消。

### 15.3 P2：SAVEPOINT 名称与回滚

- 新增公共内部上下文管理器 `savepoint(conn, name=None)`。
- 未显式传入名称时使用 `sp_` 加 UUID 十六进制后缀。
- 显式名称必须匹配 `^[a-zA-Z_][a-zA-Z0-9_]*$`，非法名称抛出 `SchedulerError("invalid_savepoint_name")`，不会执行拼接 SQL。
- 异常路径执行 `ROLLBACK TO SAVEPOINT` 后释放保存点，并保留原始业务异常。

### 15.4 P3：候选级错误隔离

- 取消操作仍按来源执行，以保持任务/建议状态与提醒取消语义一致。
- 每个提醒候选写入使用独立 SAVEPOINT。
- 同一任务中的一个候选失败，不再回滚同一任务已成功创建的兄弟候选。
- 一个建议来源失败，不阻塞其他建议；一个任务来源失败，不阻塞其他任务。
- 候选级异常计入 `error_count`，运行状态为 `partial_failed`；成功候选保持提交，失败候选不留下部分业务写入。

### 15.5 P4：created/skipped 依据实际插入结果

- `app/reminders.py` 新增 `CreateReminderResult(reminder, created)` 与 `create_reminder_result()`。
- 调度器直接尝试参数化 INSERT，并以 `dedup_key UNIQUE` 冲突作为重复判定依据；不再使用插入前 SELECT COUNT 决定 created/skipped。
- 原 `create_reminder()` 保留旧返回契约，内部委托 `create_reminder_result(...).reminder`，兼容 B 批既有调用方。
- 保证同一数据库中相同 `dedup_key` 最终只有一条提醒；不承诺跨进程 exactly-once。同进程重叠由模块级锁降低概率，跨连接/跨进程重复由数据库唯一约束兜底。

### 15.6 P5：边界与恢复测试

新增 `tests/test_reminder_scheduler_reliability.py`，覆盖：

- 结构化截止日期快照优先于 dedup key；
- 历史 malformed dedup key 不误取消；
- stalled 提醒不因截止日期变化被取消；
- 非法 SAVEPOINT 名称被拒绝且连接仍可使用；
- 同一任务一个候选失败时兄弟候选保留；
- 插入与重复插入的 `created=True/False` 契约；
- 独立 SQLite 连接重复 tick 时第二次计入 skipped；
- stale running 恰好 5 分钟不恢复、超过 5 分钟 1 秒恢复；
- weekly catch-up 恰好 24 小时边界为排他；
- 任务取消会取消活动提醒；
- 一个坏建议不阻塞健康建议；
- 一个坏任务不阻塞其他任务；
- 建议撤销采纳/逻辑过期会取消跟进提醒；
- 终态提醒不会被后续 tick 复活；
- fatal 路径 `last_error` 不泄露测试伪 token。

### 15.7 运行记录保留策略（本批只记录，不实现清理）

建议后续独立授权实现清理时采用：

- 普通历史清理只考虑最近 90 天之前的非 `running` 运行记录；
- `running` 记录不得由普通历史清理删除，应先通过恢复逻辑判断是否标记为 `abandoned`；
- 不自动删除提醒业务记录；
- 实现清理前需确认是否已有用户可见运行历史页面，以及运行记录与业务提醒之间是否存在外键或审计依赖。

### 15.8 本轮验证结果

专项可靠性测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_reminder_scheduler_reliability.py -q
```

结果：

```text
16 passed, 4 warnings
退出码：0
```

提醒相关完整专项测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py tests/test_reminder_scheduler_reliability.py -q
```

结果：

```text
67 passed, 4 warnings
退出码：0
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
386 passed, 1 skipped, 5 warnings in 87.26s (0:01:27)
退出码：0
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
COMPILE_EXIT=0
```

项目根目录未发现 `package.json`，本批未修改前端，因此没有可执行的 npm JavaScript 检查。

### 15.9 真实开发数据库核验

开始前与全量验证后均读取：

```text
data/personal_ai_os.db
Length: 258048
LastWriteTime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

结论：大小、修改时间和 SHA-256 前后一致；本轮测试均使用临时 SQLite 数据库，并同时隔离 `DATABASE_PATH` 与 `app.main.DB_PATH`。

### 15.10 保留限制与 C-C 前置条件

- 当前模块级 `threading.Lock` 只防止同一 Python 进程内重叠，不是跨进程锁。
- SQLite 唯一约束可防止重复业务提醒和重复 run key，但不应描述为分布式 exactly-once。
- 当前时区能力仍限定为项目既有工具支持的 UTC、`Asia/Shanghai` 和固定 offset；不宣称完整 IANA/DST 支持。
- C-C 在接入任何自动调度前，应明确调度宿主、单实例启动方式、锁文件或等效进程互斥策略，以及 API/前端如何展示 `partial_failed` 与脱敏错误。

**1.3.5-C-B-R 到此停止，等待人工审核；未自动进入 C-C。**

---

## 16. 1.3.5-C-C-0：自动调度前可靠性补强（2026-10-13）

### 16.1 本批范围

本批只补自动调度接入前的可靠性测试缺口，不接入 FastAPI startup/shutdown，不创建后台常驻循环、定时器或自动调度入口，不新增 API、前端、外部通知或 LLM 调用。

本批未修改生产逻辑；现有生产代码已满足新增测试验证的契约，因此只修改测试文件并追加本文档记录。

### 16.2 新增和加强的测试

修改文件：

- `tests/test_reminder_scheduler_reliability.py`

新增/加强内容：

1. **SAVEPOINT 内异常完整回滚**
   - `test_savepoint_rolls_back_inner_write_preserves_outer_and_keeps_original_exception`
   - 验证保存点外记录保留、保存点内记录回滚、原始 `sqlite3.DatabaseError` 类型和消息继续向外传播，且同一连接后续 SQL 可继续执行。

2. **weekly 24 小时排他边界**
   - 加强 `test_weekly_catchup_exact_24_hour_boundary_is_exclusive`。
   - 除保留 `created_count == 0` 返回值断言外，直接查询 `reminders` 表中 `review.weekly_due_v1` 记录数，确认恰好 24 小时及 24 小时后 1 分钟均未创建提醒。

3. **候选成功写入后的 fatal 错误语义**
   - `test_fatal_after_candidate_persisted_marks_failed_run_without_hiding_reminder`
   - 在候选生成后、`_advance_due` 推进阶段注入 fatal SQLite 异常。
   - 验证运行记录为 `failed`，已成功创建的业务提醒仍保留为 `scheduled`，`created_count=1`、`due_count=0`、候选级 `error_count=0`，fatal 信息写入脱敏后的 `last_error`，异常响应也不包含测试伪 token。
   - 该测试明确当前语义：fatal 错误不回滚此前已释放保存点并提交的候选写入；这不是“全部业务失败”，C-C 展示层必须分别表达运行失败与已持久化业务结果。

4. **相同 dedup key 的真实同步竞争**
   - `test_concurrent_connections_create_same_dedup_key_exactly_once`
   - 使用两个线程、两个独立 SQLite connection、同一个临时数据库文件和 `threading.Barrier(2)` 同时进入创建窗口。
   - 两个线程均设置 5 秒 busy timeout，最终验证：
     - 一个结果为 `created=True`；
     - 另一个结果为 `created=False`；
     - 两条结果指向同一 reminder ID；
     - 数据库中相同 `dedup_key` 最终只有一条记录。
   - 该测试验证同机多连接/多线程下相同业务提醒的唯一约束冲突处理；不等同于跨进程调度 run 级互斥。

### 16.3 中间失败与处理

新增测试首次运行时出现 2 个失败，均为测试构造问题，不涉及生产代码修改：

- fatal 测试最初误把推进阶段 fatal 错误计入候选级 `error_count`；根据实际事务契约修正为：候选级错误为 0，fatal 错误记录在运行状态与 `last_error` 中。
- 并发测试最初在关闭 setup connection 后继续读取 settings，导致 `Cannot operate on a closed database`；调整为在关闭前读取 settings。

修正后可靠性专项测试通过。

### 16.4 验证结果

可靠性专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminder_scheduler_reliability.py -q
```

结果：

```text
19 passed, 4 warnings
退出码：0
```

提醒相关完整专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py tests/test_reminder_scheduler_reliability.py -q
```

结果：

```text
70 passed, 4 warnings
退出码：0
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

结果：

```text
389 passed, 1 skipped, 5 warnings in 95.06s (0:01:35)
退出码：0
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
COMPILE_EXIT=0
```

### 16.5 数据库保护

测试前后真实开发数据库均为：

```text
data/personal_ai_os.db
Length: 258048
LastWriteTime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

所有新增测试继续使用 `tmp_path` 临时 SQLite 文件，并同时设置 `DATABASE_PATH` 与 monkeypatch `app.main.DB_PATH`。未对真实开发数据库执行迁移、初始化、tick 或写入。

### 16.6 剩余风险与 C-C 后续前置

- 本批证明相同 `dedup_key` 在两个独立连接同步竞争下最终只产生一条业务提醒，但没有实现调度 run 级跨进程互斥。
- 多个进程仍可能以不同 `run_key` 同时扫描；数据库唯一约束防止重复提醒，不保证全局只有一个 run 或汇总计数完全一致。
- C-C-1/C-C-2 接入自动调度前仍需设计单实例锁/租约、stale lock 恢复、手动与自动触发冲突策略，以及 `failed`/`partial_failed` 与已持久化提醒的展示语义。

**1.3.5-C-C-0 到此停止；尚未接入自动调度，等待人工审核后再进入后续 C-C 批次。**

## 17. 1.3.5-C-C-2a：SQLite 跨进程调度锁服务（2026-10-13）

### 17.1 本批范围

本批只实现独立的 SQLite 调度租约服务及测试，不把租约接入 `reminder_scheduler.run_tick()`，不接入 FastAPI startup/shutdown，不启动自动调度。

新增文件：

- `app/reminder_scheduler_lock.py`
- `tests/test_reminder_scheduler_lock.py`

未修改：

- `app/reminder_scheduler.py` 的 run 执行逻辑；
- `app/main.py` 的生命周期和 API；
- 前端、依赖配置、真实开发数据库、Git 状态。

### 17.2 租约表与约束

`init_lease_db(conn)` 幂等创建单例表 `reminder_scheduler_leases`：

- `scope TEXT PRIMARY KEY CHECK (scope='global')`：数据库级 CHECK 约束保证只有一个全局租约行；
- `owner_token TEXT NOT NULL DEFAULT ''`：持有者标识，最大长度 120；
- `lease_token TEXT NOT NULL DEFAULT ''`：每次获取生成的随机租约 token，最大长度 80；
- `fencing_token INTEGER NOT NULL DEFAULT 0`：单调递增 fencing 序号，数据库约束要求非负；
- `acquired_at`、`heartbeat_at`、`expires_at`：UTC ISO-8601 毫秒精度字符串；
- `released_at TEXT`：正常释放时间；
- `pid INTEGER`、`hostname TEXT NOT NULL DEFAULT ''`：诊断信息，hostname 最大长度 120。

初始化使用 `INSERT OR IGNORE` 创建空闲单例行：`owner_token=''`、`lease_token=''`、`fencing_token=0`。重复初始化不增加行。

### 17.3 API 契约

核心结果状态：

- `acquired`：成功获取、续租或释放当前持有者自己的租约；
- `busy`：另一个未过期持有者仍有效；
- `lost`：调用方提供的 `owner_token`、`lease_token`、`fencing_token` 不匹配，或租约已过期/被接管；
- `database_error`：SQLite 或操作系统级数据库错误；结果不携带 lease。

主要函数：

- `configure_connection(conn, busy_timeout_ms=...)`：设置 `sqlite3.Row`、`PRAGMA foreign_keys=ON` 和 busy timeout；
- `init_lease_db(conn)`：幂等建表和插入空闲单例；
- `acquire_lease(conn, now=..., ttl_seconds=30.0, owner_token=..., pid=..., hostname=..., busy_timeout_ms=2000)`；
- `heartbeat_lease(conn, owner_token=..., lease_token=..., fencing_token=..., now=..., ttl_seconds=...)`；
- `release_lease(conn, owner_token=..., lease_token=..., fencing_token=..., now=...)`；
- `current_lease(conn)`：只读读取当前租约行。

参数级错误（无时区时间、错误类型、TTL 超出 1..3600 秒、非法身份标识等）在进入数据库写事务前抛出 `LeaseError`；数据库错误才返回 `database_error`。

### 17.4 竞争、过期与 fencing 语义

- 获取和过期接管使用 `BEGIN IMMEDIATE` 取得 SQLite 写事务，再执行条件 `UPDATE`。
- 过期判断为半开区间：`expires_at <= now` 即视为过期；恰好在过期时刻，旧持有者不能续租。
- 首次获取或过期接管都会执行 `fencing_token=fencing_token+1`。
- 续租要求 `owner_token`、`lease_token`、`fencing_token` 完全匹配且 `expires_at > now`。
- 释放要求身份三元组完全匹配且此前未释放；重复释放或身份失效返回 `lost`，不会复活或覆盖新持有者。
- `busy timeout` 只让 SQLite 短暂等待写锁；等待后仍失败时返回 `database_error`，不会误报获取成功。

### 17.5 已验证的测试场景

`tests/test_reminder_scheduler_lock.py` 共 14 个测试，覆盖：

1. 租约表幂等初始化和单例空闲行；
2. 首次获取成功，同 owner/其他 owner 在租约有效时得到 `busy`；
3. heartbeat 必须同时匹配 owner、lease token 和 fencing token；
4. 条件释放后新 owner 可获取，且 fencing token 递增；
5. 半开过期边界、过期接管、旧 owner heartbeat/release 失效；
6. 两个线程、两个独立 SQLite connection、同一临时库文件、同步屏障下竞争过期租约，最终一个 `acquired`、一个 `busy`，租约表仍只有一行；
7. 其他连接持有写锁且 busy timeout 超时时返回 `database_error`；
8. naive datetime、非法 TTL 抛出 `LeaseError`；
9. 通过 `app.main.init_db()` 初始化后，锁服务仍可独立幂等初始化；
10. 两个真实 OS 子进程竞争首次获取，结果为一个 `acquired`、一个 `busy`；
11. 两个真实 OS 子进程竞争过期接管，结果为一个 `acquired`、一个 `busy`，且 fencing token 从 1 增至 2；
12. 获取租约时注入 COMMIT 失败，返回 `database_error` 且不假报 `acquired`，后续全新连接仍可正常获取；
13. 旧 owner 在新 owner 接管后释放失败，不覆盖或删除新 owner；
14. 两个独立连接并发初始化租约表，最终仍只有一个空闲单例行。

跨进程测试使用每个子进程独立 stdin 作为启动屏障，避免 Windows 下匿名管道 fd 继承/关闭时序差异。

### 17.6 实际验证结果

锁专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminder_scheduler_lock.py -q
```

结果：

```text
14 passed, 4 warnings in 2.49s
退出码：0
```

提醒相关完整专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py tests/test_reminder_scheduler_reliability.py tests/test_reminder_scheduler_lock.py -q
```

结果：

```text
84 passed, 4 warnings in 17.28s
退出码：0
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

结果：

```text
403 passed, 1 skipped, 5 warnings in 96.51s (0:01:36)
退出码：0
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
退出码：0
```

测试警告仅包含既有 FastAPI `on_event` 弃用警告和既有 Starlette/httpx TestClient 弃用提示；本批未为消除警告做无关升级。

### 17.7 数据库保护

测试均使用 `tmp_path` 临时 SQLite 文件，并同时：

- 设置 `DATABASE_PATH`；
- monkeypatch `app.main.DB_PATH`。

真实开发数据库 `data/personal_ai_os.db` 执行前后核验一致：

```text
Length: 258048
LastWriteTime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

未对真实开发数据库执行初始化、迁移、锁获取、续租、释放或 tick。

### 17.8 当前边界和后续前置

本服务已经提供**调度权租约归属层面**的跨进程互斥，但尚未提供完整自动调度保护：

- `run_tick()` 尚未获取、续租或释放该租约；
- `reminder_scheduler_runs` 尚未要求写入当前 fencing token；
- reminders 的创建/状态推进条件更新尚未携带 fencing token；
- 旧持有者在租约过期后仍可能在未 fencing 的业务写入中完成数据库操作；业务 `dedup_key UNIQUE` 能防止重复提醒，但不能单独保证旧持有者所有写入被拒绝；
- 自动 tick、手动 tick、recovery tick 的冲突策略尚未接入；
- 运行记录保留策略尚未实现。

因此，C-C-2b/后续批次必须先把租约生命周期接入 run，并把 fencing token 用于 run/reminder 条件写入，之后才能启用自动调度。

**1.3.5-C-C-2a 到此停止；当前仍未接入自动调度，等待人工审核后再进入后续批次。**

## 18. 1.3.5-C-C-2b：租约接入调度核心与 Fencing 写入保护（2026-10-13）

### 18.1 本批范围

本批在 C-C-2a 的 SQLite 租约锁服务基础上，把租约接入提醒调度核心 `run_tick()`，并为调度器拥有的关键业务写入加入 fencing 校验。

本批仍不启用自动调度：

- 未接入 FastAPI startup/shutdown；
- 未新增后台线程、定时器或常驻 tick；
- 未新增 HTTP API 或前端；
- 未调用 LLM，未发送外部通知；
- 未修改任务、目标、记忆、画像；
- 未安装依赖；
- 未初始化 Git、提交、推送或部署。

修改文件：

- `app/reminder_scheduler.py`
- `app/reminder_scheduler_lock.py`
- `app/main.py`
- `tests/test_reminder_scheduler_fencing.py`
- 本文档

### 18.2 Schema 兼容性

`reminder_scheduler_runs` 新增 nullable 字段：

```text
lease_fencing_token INTEGER
```

新建数据库直接包含该字段；旧数据库由 `_migrate_reminder_scheduler_runs()` 使用幂等的 `ALTER TABLE ADD COLUMN` 补齐。旧运行记录该字段为 `NULL`，仍可按历史记录查询，不参与新调度 run 的 fencing 条件更新。

`reminder_scheduler_runs.owner_token` 继续保存当前 run 的持有者标识；`lease_fencing_token` 保存创建该 run 时的 fencing token。

### 18.3 租约生命周期

`run_tick()` 的顺序为：

1. 校验带时区的 `now`、触发类型和 `run_key`；
2. 获取进程内 `threading.Lock`，该锁只减少同进程重叠；
3. 确认 SQLite 外键已开启；
4. 对 Python sqlite3 隐式事务模式下调用方遗留的 pre-tick 写事务执行 `commit()`，再获取跨进程租约；
5. 通过 `reminder_scheduler_lock.acquire_lease()` 获取全局租约；
6. 获取失败时直接返回/抛出明确的租约错误，不创建 `running` run，不扫描提醒，不执行业务写入；
7. 获取成功后执行 stale run 恢复、创建 run、过期处理、候选生成、状态推进和 run 结束更新；
8. finally 阶段用完整 `owner_token + lease_token + fencing_token` 条件释放租约。

租约竞争结果：

- 当前持有者有效：竞争者抛出 `LeaseBusyError`，错误码 `lease_busy`，HTTP 语义 409；
- SQLite busy/locked 或其他数据库错误：抛出 `LeaseDatabaseError`，错误码 `lease_database_error`，HTTP 语义 503；
- tick 执行期间身份失效或被更高 fencing token 接管：抛出 `LeaseLostError`，错误码 `lease_lost`，HTTP 语义 409。

`release_lease()` 使用释放瞬间的 UTC 时间作为 `now`，这是清理动作的实际时间；调度业务确定性仍由 `run_tick(now=...)` 的显式参考时间控制。

### 18.4 Fencing 写入保护

新增调度器内部 guard：

```python
{
  "owner_token": "...",
  "lease_token": "...",
  "fencing_token": 1,
}
```

受保护写入统一通过：

- `guarded_write()`
- `create_reminder_guarded()`
- `transition_reminder_guarded()`
- `expire_reminders_guarded()`
- `_create_run(..., guard=...)`
- `_finish_run(..., guard=...)`
- `recover_stale_runs(..., guard=...)`

每个受保护写入均：

1. 开启 `BEGIN IMMEDIATE`；
2. 在同一写事务内读取 `reminder_scheduler_leases` 单例行；
3. 校验 `owner_token`、`lease_token`、`fencing_token` 完全一致；
4. 校验租约未释放、未过期；
5. 执行对应业务 SQL；
6. 提交前再次校验同一租约身份；
7. 校验通过才 `COMMIT`，否则回滚并抛出 `LeaseLostError`。

已纳入保护的调度写入：

- stale `running` run 标记为 `abandoned`；
- 新 run 的创建；
- run 结束状态更新；
- 调度器触发的提醒过期；
- 调度候选提醒创建；
- 调度器触发的 mark-due；
- 调度器触发的取消；
- quiet hours 下的过期处理；
- 任务/建议来源失效引发的提醒取消。

用户直接调用的 `app.reminders` 存储函数仍保持原服务语义；它们不是调度器写入，不要求持有调度租约。调度器内部不得绕过 guarded helper 调用这些函数执行写操作。

### 18.5 已由测试证明的性质

新增 `tests/test_reminder_scheduler_fencing.py`，共 6 个测试：

1. 有效租约被其他连接持有时，竞争者 `run_tick()` 抛出 `LeaseBusyError`，数据库中没有 run、没有提醒，当前 owner 不改变；
2. 旧持有者过期并被新 owner 接管后，旧 guard 创建提醒会被拒绝，数据库没有对应 `dedup_key`；
3. 旧 guard 不能把已有提醒推进为 expired，提醒保持 scheduled；
4. 旧 guard 不能结束自己早先创建的 running run，run 保持 running，fencing token 保持旧值；
5. 两个独立 SQLite 连接通过线程屏障同时调用 `run_tick()`，最终只有一个 run，且租约已释放；
6. 两个真实 OS 子进程通过各自 stdin 屏障同时调用 `run_tick()`，最终一个执行、一个 busy，数据库只有一个 succeeded run，run 的 fencing token 为 1。

既有调度、恢复、可靠性和锁测试继续覆盖：

- run_key 幂等；
- 候选 SAVEPOINT 隔离；
- fatal 后已提交提醒保留；
- stale running 5 分钟边界；
- daily/weekly catch-up 边界；
- quiet hours；
- 任务/建议来源失效取消；
- SQLite 租约过期半开边界；
- fencing token 在租约表内单调递增；
- owner/lease/fencing 三元组校验。

### 18.6 验证结果

Fencing 专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminder_scheduler_fencing.py -q
```

结果：

```text
6 passed, 4 warnings in 2.54s
退出码：0
```

提醒相关完整专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_reminders_store.py tests/test_reminder_rules.py tests/test_reminder_state_machine.py tests/test_reminder_scheduler.py tests/test_reminder_quiet_hours.py tests/test_reminder_scheduler_recovery.py tests/test_reminder_scheduler_reliability.py tests/test_reminder_scheduler_lock.py tests/test_reminder_scheduler_fencing.py -q
```

结果：

```text
90 passed, 4 warnings in 19.80s
退出码：0
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

结果：

```text
409 passed, 1 skipped, 5 warnings in 106.57s (0:01:46)
退出码：0
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：

```text
退出码：0
```

警告仍为既有 FastAPI `on_event` 和 Starlette/httpx TestClient 弃用提示，本批未做无关升级。

最终复核时，将独立连接竞争测试中的一个恒真 owner 断言收紧为数据库最终状态断言：唯一 run 的 `owner_token` / `lease_fencing_token` 必须与已释放租约一致；收紧后 fencing 专项结果为 `6 passed, 4 warnings in 2.54s`，退出码 0。

### 18.7 中途发现与处理

- 初版接线后，异常类定义顺序导致测试收集失败；已调整 `SchedulerError` 定义顺序。
- Python sqlite3 默认隐式事务连接可能在 `update_settings()` 后遗留未提交事务，导致租约服务显式 `BEGIN IMMEDIATE` 失败；`run_tick()` 在获取租约前提交该 pre-tick 调用方事务。
- `init_lease_db()` 增加在已有事务中只执行幂等 schema DML/DDL、不重复开启事务的兼容路径；独立锁服务测试仍全部通过。
- 初版调度专项一度出现 4 个失败，定位为租约释放时间和遗留事务处理问题；修复后调度相关 40 项测试恢复通过。

### 18.8 真实数据库保护

测试均使用 `tmp_path` 临时 SQLite 文件，并同时设置：

- `DATABASE_PATH`
- monkeypatch `app.main.DB_PATH`

真实开发数据库执行前后一致：

```text
data/personal_ai_os.db
Length: 258048
LastWriteTime: 2026-10-12 16:45:50
SHA-256: 3EEB309180E5644D92A39CCD23C145A568373743266C290E8187C67079FEBCEC
```

未对真实开发数据库执行初始化、迁移、租约获取、续租、释放、tick 或业务写入。

### 18.9 当前语义边界和 C-C-2c 前置

本批保证：在同一 SQLite 数据库中，调度器拥有的写入必须在当前有效 fencing guard 的写事务内提交；旧持有者被新 owner 接管后，不能再创建提醒、推进提醒状态、恢复旧 run，或把自己的旧 run 标记为终态。

仍需在后续批次处理：

- 自动 tick 尚未接入 FastAPI 生命周期，也没有后台循环；
- 租约 TTL 固定为 30 秒，当前 tick 每个写操作独立校验租约，但没有长 tick 的后台心跳线程；超长 tick 可能在后续写操作处收到 `lease_lost`，已提交的部分业务结果会保留；
- 手动 tick API/调用方如何返回 `lease_busy`、`lease_lost` 和 `lease_database_error`，尚待接口层批次定义；
- recovery tick 与正常 tick 共用租约，但还没有 API 层触发策略；
- run 记录保留策略尚未实现；
- 当前不承诺跨业务大事务 exactly-once；已提交提醒、run 状态和候选去重仍按既有多提交边界解释。

**1.3.5-C-C-2b 到此停止；调度核心已受租约与 fencing 保护，但自动调度仍未启用，等待人工审核后再进入 C-C-2c。**



