# 技术决策记录

## 2026-10-22：凭据 UI 遵循 write-only 纪律，编辑表单不含 secret

### 背景

凭据链路后端已完备（Fernet 落库、明文不回显），但前端零接入；`DataSourceIn.secret` 只在创建时存在，`PATCH` 无 secret 字段。

### 决策

- 前端凭据输入只写不读：`type="password"`、无 `:value` 回显、提交后立即清空 ref。不实现"查看已存凭据"——后端没有解密回显端点，加一个就破坏了"库文件泄露不暴露凭据"的威胁模型。
- 更换凭据 = 直接 `PUT` 覆盖；清除 = 专用 `DELETE` + 内联二次确认。不在编辑表单里放 secret 字段：PATCH 全量替换语义会让"没填 secret"产生歧义，专用端点无此问题。
- 已配置/未配置来自 `has_credential` 布尔值，界面永不接触密文。

### 代价

用户忘记凭据内容时只能更换，无法找回——write-only 的固有属性，已在界面文案标注"保存后不可再查看"。

更新时间：2026-10-22 11:55 CST

## 2026-10-22：调度器对 SQLITE_READONLY 做有界重试，BUSY/LOCKED 不重试

### 背景

"flaky" 测试 `test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 实为真实缺陷：全新 WAL 库上调度线程的首个写连接与其它连接的打开/关闭竞争时，Windows 返回瞬时 `SQLITE_READONLY`，tick 被整体跳过，到期提醒延后一个轮询周期（默认 60s）。复现与机制详见 `docs/testing.md` 0.6 节。

### 方案对比

- **方案 A：测试侧消除竞争（审计轮询改长连接）**。测试全绿但产品缺陷仍在，真实部署的请求连接同样触发。
- **方案 B：busy_timeout 覆盖**。READONLY 不是锁等待，加长超时无效。
- **方案 C（采用）：租约获取阶段标记 retryable，`run_scheduled_tick` 换连接重试**。READONLY 是瞬时态（实测新连接立即成功）；重试前进程未 COMMIT 任何东西，无重复风险；同一 `run_key` 使重放幂等。

### 关键取舍

- 重试集合**只含 READONLY**。BUSY/LOCKED 已由 `busy_timeout=5000` 覆盖，若也重试会把等待叠成 2s×N——写锁测试的耗时曾因此从 1.2s 涨到 5.3s，随后收窄。
- 重试至多 4 次、退避 0.05/0.15/0.4s，最坏增加 0.6s，远小于一个轮询周期。
- 不改状态机、不吞异常：重试耗尽后仍抛 `LeaseDatabaseError`，由既有分类与日志承接。

### 代价

`LeaseResult`/`LeaseDatabaseError` 各加一个字段；契约由 `tests/test_reminder_scheduler_retry.py` 7 例固定（重试成功、永久错误立即抛、次数有界、run_key 幂等、分类器正反例）。

## 2026-10-22：画像条目日期字段的"清空"语义（后端两行修复）

### 背景（冒烟暴露的真实缺陷）

`PATCH /api/profile-items/{id}` 原用 `if body.valid_from is not None` 判断是否更新日期字段。JSON `null` 与缺省键在 Pydantic 解析后都是 `None`，于是**一旦设置了日期，就无法通过 API 清空**——浏览器编辑表单清空日期提交 `valid_from: null` 后，`valid_from` 仍是原值（冒烟 `清空日期提交 null` 首次 FAIL 暴露）。

### 决策

改为 `if "valid_from" in body.model_fields_set`（`valid_until` 同理）：缺省键 → 不更新；显式 `null` → 清空。这与同文件 outcome 端点的既有模式（`app/main.py:3341` `data.model_fields_set`）一致，属于把后端自家的不一致拉齐，不是新语义。

### 理由

- 前端"清空日期"是正常编辑需求，绕开它（例如约定魔法值 `""`）会把缺陷转移到契约里。
- outcome 端点已经证明 `model_fields_set` 模式在本项目可行，两处统一后未来的日期类字段有明确先例可循。

### 代价与守卫

- 行为变更：此前传 `null` 是静默 no-op，现在会真的清空。检查过全部既有测试（无依赖该 no-op 的断言）。
- 契约测试 `test_patch_explicit_null_clears_date_but_omitted_key_keeps_it` 同时固定"缺省保留 / 显式 null 清空"两个方向。
- 回滚：还原 `temp/bak-main-py-005.py` 中 `update_profile_item` 的两处 if 即可。

## 2026-10-22：画像条目的编辑入口按状态矩阵分发

### 决策

`Persona.vue` 的动作行由 `item.status` 决定：pending→确认/拒绝；confirmed→过期/编辑/删除；expired→编辑/删除；rejected→无动作（只读说明）。

### 理由

- 状态机完全在服务端（`_transition_profile_item` 的 expected 校验，409），前端如果渲染出服务端必拒的按钮，用户必然踩到 409。
- rejected 条目默认从列表隐藏、显式 `?status=rejected` 才可见——「已拒绝」tab 保证被拒条目可审计、可追溯。

## 2026-10-22：两套写语义并存，各自用测试钉住

### 背景

同一个仓库里存在两种 PATCH/PUT 语义：

| 接口 | 语义 | 依据 |
| --- | --- | --- |
| `PATCH /api/projects|goals|tasks/{id}` | 全量替换（省略字段回默认值） | `app/main.py:1995-2000` |
| `PUT /api/recommendations/{id}/outcome` | 部分更新（`exclude_unset=True` + 服务层 merge） | `app/main.py:3340`、`app/recommendation_feedback.py:526` |

### 决策

不统一两者。工作项按"替换"处理（前端发完整 body），outcome 按"补丁"处理（只发改动字段 + 完整指标列表）。两套语义分别由 `tests/test_work_ui.py` 与 `tests/test_recommendation_insights_ui.py` 中的契约测试固定，任一侧漂移都会立刻变红。

### 理由

- 统一会改动既有 API 语义，影响面超出前端补齐的范围；而两套语义各自都自洽（替换适合"编辑表单整体保存"，补丁适合"只改一个评级"）。
- 真正的风险不是语义不同，而是**调用方记错语义**。用契约测试把两种行为写进断言，比统一实现更能防止错误扩散。

### 代价

新人需要先读这两组测试才能动手；`docs/current-task.md` 与 `specs/003|004` 都在开头标明差异。

## 2026-10-22：比率统一走 `fmtRatio()`，兼容 null 与对象两种形状

### 背景（实现中发现的真实偏差）

`app/recommendation_stats.py:118` 的 `_ratio()` 在分母为 0 时返回 `None`，否则返回 `{"numerator", "denominator", "value", "sample_status"}`——**不是标量**。`RecommendationInsights.vue` 初版按标量渲染（`Number(v) * 100`），会让所有有效比率静默变成「—」（`NaN` 分支），页面看起来"正常"但数字全错。

### 决策

1. `fmtRatio()` 同时接受 `null` 与对象，对象取 `.value`。
2. 对象形状额外展示 `numerator/denominator`，让"0/2 条有反馈"这类信息可见。
3. 新增测试同时断言两种形状（`test_ratio_renderer_handles_null_and_object_shape`、`test_statistics_counts_seeded_rows_and_rejects_bad_range`）。

### 理由

「—」与「0.0%」语义不同：前者是"没有观测"，后者是"观测到 0"。把两者混为一谈会让统计面板失去可信度。

## 2026-10-22：洞察组件自加载，不进父级 `load()`

### 决策

`OutcomeForm` 与 `RecommendationInsights` 各自在 `onMounted` 拉自己的数据，父级 `Recommendations.vue` 只负责显示/隐藏。

### 理由

父级 `load()` 已经在为每条建议并行拉 feedback；再叠加 outcome 会变成二维 Promise 矩阵，且 outcome 只有 `accepted` 才需要。拆开后父级只加 3 行接入代码。

### 代价

每次展开统计面板都重新请求（7 个聚合查询）。单用户本地应用可接受，不引入缓存层。

## 2026-10-22：工作项写操作采用「前端全量 body」而非改后端部分更新

### 背景

`PATCH /api/projects|goals|tasks/{id}` 复用 POST 的 Pydantic 模型并执行全字段 UPDATE（`app/main.py:1995-2000`）。证据（`tests/test_work_ui.py` 固化）：

- 只发 `{"title","status"}` → `detail=""`、`due_date=null`、`project_id=null`、`priority="normal"`。
- 只发 `{"name","status"}` → 项目 `description`/`tech_stack`/`repo_url` 全被清空。

### 决策

1. 不改后端语义（不引入 `model_dump(exclude_unset=True)` 部分更新），前端新增 `projectBody`/`goalBody`/`taskBody` 三个全量映射函数，所有 POST/PATCH 必须经它们。
2. 状态快捷动作（开始/完成/重开/取消、目标完成/恢复/放弃）同样走完整 body，禁止只发 `status`。
3. 用测试把契约钉死：4 个后端契约测试 + 前端源码断言（三函数字段完整性、快捷动作调用形式）。

### 理由

- 改后端部分更新会改变既有 API 语义，影响面覆盖所有调用方与 12 个既有测试，属于本特性范围外的行为变更。
- 全量替换语义本身是合理的（PATCH 语义偏向替换），问题只在于调用方必须显式构造完整对象；把责任固定在三个函数上，比散落在 12 个 `api()` 调用点可靠。

### 边界与代价

- 未来若改为部分更新，`tests/test_work_ui.py` 的契约测试会变红——这是预期的提醒信号，届时同步更新语义与测试。
- 前端多一层映射，字段新增必须同步三个函数；`test_body_mappers_cover_every_backend_field` 会在漏字段时报错。

## 2026-10-22：工作项列表用前端过滤，不按 tab 请求后端

### 决策

项目/目标/任务三个区块一次拉全量，状态 tab 在前端 `computed` 过滤。

### 理由

- 单用户本地应用，三张表数量级在千以内；每次切 tab 都请求会让列表闪烁。
- 任务的 PATCH 响应不含 `project_name`（JOIN 列），前端过滤 + 局部合并可以避免切换后名称丢失。
- 项目接口本就不支持 `status` 参数，若只对目标/任务走后端过滤会造成三处实现不一致。

### 代价

数据量增长到万级时需要改回服务端过滤 + 分页；已在 `docs/progress.md` 的未开始项保留分页话题。

## 2026-10-22：Playwright 锚点用 `name` 与 `data-section`

### 决策

表单控件统一加 `name` 属性，三个列表区块加 `data-section="projects|goals|tasks"`，e2e 脚本以此定位。

### 理由

- 首次冒烟失败暴露真实歧义：任务卡片里显示 `project_name`，页面级 `hasText: "E2E 项目"` 会同时匹配项目卡片和任务卡片；项目详情区的「目标 (0)」标题又让 `section:has-text("目标 (")` 同时命中项目 section。两次都是**选择器歧义**，不是产品缺陷。
- `name` 与 `data-section` 是语义化属性，不改变样式与行为，比 XPath 或用例内硬编码 DOM 结构稳定。

## 2026-10-21：提醒接口与租约时钟缺陷修复批次

### 背景

全项目审查（`docs/audits/2026-10-21-full-project-review.md`）发现四项缺陷：设置接口字段白名单与领域模型不一致、`ReminderError.message` 不存在导致 500、租约释放使用真实墙钟而获取使用注入时钟、`snooze` 不写 `snoozed_until`。

### 决策

1. `PUT /api/reminder-settings` 不再维护独立白名单，改为以 `reminders.SETTINGS_FIELDS` 为唯一真源；`created_at`/`updated_at` 作为只读回显字段忽略，其余未知字段返回 400。
2. 三处 `detail=exc.message` 改为 `detail=str(exc)`。
3. `run_tick` 释放租约改用本次 tick 的 `current_dt`；同时 `_lease_is_free` 与 acquire 的条件 UPDATE 把 `released_at IS NOT NULL` 视为可用，使"已释放即空闲"不再依赖调用方时钟。
4. `snooze` 写入 `snoozed_until = now + snooze_minutes`，默认 10 分钟，上限 7 天；HTTP body 支持 `snooze_minutes`。

### 理由

- 字段名单放在路由层必然与领域模型漂移，已经造成"前端保存 500 / 正确字段静默丢弃"。
- 租约可用性不应由调用方时钟决定；`_begin_guarded_write` 早已把 `released_at` 视为"已失去租约"，获取侧与之对齐即可。
- 没有唤醒时间的 `snoozed` 会让调度器在下一次 tick 立即回弹为 `due`，等于功能不存在。

### 边界

- 不改变 fencing 语义：release 仍然只允许匹配 owner/lease/fencing 且未释放的一方执行，fencing token 仍然单调递增。
- 跨"时间倒流"的补跑（逻辑时间早于上次释放时间）现在可用，但租约 TTL 检查仍按调用方提供的 `now` 比较。

## 2026-10-21：SQLite 日志模式以 WAL 为准

### 背景

`init_db()` 每次启动执行 `PRAGMA journal_mode = WAL`，而 README 与 `docs/database.md` 声称真实库是 delete journal 模式，并说"未来若切换 WAL 才需要备份 `-wal`/`-shm`"。文件头字节显示当前库（write/read version = 1/1）确实还没切换，所以下一次启动会静默改变备份语义。

### 决策

保留 WAL（HTTP 请求、提醒调度 worker、通知消费 worker 是多写者，rollback journal 下更容易 `database is locked`），并同步修正文档与备份指引：

- 启动日志打印实际 `journal_mode`；
- 备份必须停服后同时复制 `db`、`-wal`、`-shm`，或用 `VACUUM INTO` 取一致快照；
- 不再把 delete journal 描述为当前状态。

### 否决

- 去掉 WAL 设置：与 `synchronous=NORMAL` 的取舍不匹配，且多写者争用会显著增加。
- 增加 `PAOS_SQLITE_JOURNAL_MODE` 开关：当前无排障需求，先不引入第二种配置面。

## 2026-10-21：并发测试改用 rollback journal

### 背景

4 个"多线程首次写"用例在不同顺序下随机报 `attempt to write a readonly database`。最小复现（与项目代码无关）：

```text
全新 WAL 库 + 3 线程首次并发 BEGIN IMMEDIATE → 2/8 轮 readonly
同一 WAL 库连续 12 轮并发写                    → 0/12
同样场景改用 DELETE journal                    → 0/8
```

即失败发生在**全新 WAL 库首次建立 `-shm`/`-wal` 的竞争窗口**，不是唯一约束或 fencing 逻辑。

### 决策

这些用例只关心去重约束与租约互斥，不关心 WAL 引导，因此在建库后切到 rollback journal（`tests/conftest.py::rollback_journal` fixture）；生产仍使用 WAL。生命周期用例 `test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db` 无法切换（lifespan 内会重新设置 WAL），改为全程持有一个 warm 连接。

### 边界

不把该现象当作产品缺陷；但**首次启动全新库**时若 HTTP 请求与调度 worker 同时写，仍可能命中同一竞争窗口，属于已知风险，记录为后续可用"进程级常驻连接"消除。

## 2026-10-13：提醒调度跨进程互斥使用 SQLite 租约和 fencing token

### 背景

本地单实例架构需要允许多个 Python 进程同时存在，但不能让多个进程同时执行提醒调度写入。仅靠进程内 `threading.Lock` 或 `asyncio.Lock` 无法跨进程互斥。

### 决策

C-C-2a/2b 采用 SQLite 全局单例租约表：

- acquire 使用条件原子写入获取有效租约或接管过期租约；
- fencing token 单调递增；
- heartbeat 只能延长完全匹配 owner / lease / fencing 且尚未过期的租约；
- release 只能释放完全匹配 owner / lease / fencing 的未释放租约；
- 调度关键写入在事务中再次校验租约身份并在提交前复查。

### 理由

- 不引入 Redis、消息队列或额外依赖；
- 与当前 SQLite 单数据库架构兼容；
- 可在 Windows 和本地单机场景工作；
- 旧 owner 被新 owner 接管后，不能仅靠 Python 开头检查继续提交受保护写入。

### 边界

该机制不提供跨所有业务对象的 exactly-once，也不把普通非调度写入全部纳入 fencing。提醒业务去重仍依赖 `dedup_key UNIQUE`，run 幂等仍依赖 `run_key UNIQUE`，调度互斥依赖 lease。

## 2026-10-13：C-C-2b 不启用自动调度

### 决策

C-C-2b 只把租约接入调度核心和受保护写入，不接入 FastAPI startup/shutdown，不创建后台循环。

### 理由

自动生命周期还需要明确多 worker、热重载、关闭超时、同步 SQLite 调用边界、长 tick heartbeat 和恢复策略。直接启动后台循环会跨越尚未审查的安全边界。

## 2026-10-13：C-C-2c-0 保持只读设计审查

### 决策

C-C-2c-0 只允许新增设计文档，不修改生产代码、测试或 schema，不运行 `run_tick()`。

### 理由

需要先审核自动调度架构、长任务续租和故障测试矩阵，再决定 C-C-2c 的最小实现。

## 2026-10-13：C-C-2c-0 设计审查完成，产出推荐架构（待人工审核）

### 决策

完成只读设计审查并创建 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md`。文档给出推荐方案（尚未经人工批准，属于设计建议而非已实施决策）：

- 新增 `ReminderSchedulerService`，由 FastAPI lifespan 管理 asyncio 控制循环，同步 `run_tick()` 在专用单线程 executor 中执行、使用独立 SQLite 连接。
- 长 tick 续租采用候选项/阶段边界显式 heartbeat（复用 `heartbeat_lease()`）+ tick 软上限，否决独立心跳线程与跨线程共享 SQLite 连接。
- 自动调度默认关闭或显式启用；多 worker 下允许所有进程启动循环，但仅当前有效租约持有者执行 `run_tick()`。

### 理由

自动生命周期涉及多 worker、热重载、关闭超时、同步 SQLite 调用边界与长 tick 续租，必须先完成设计审查并经人工批准才能进入实现，避免跨越尚未审查的安全边界。

### 边界

以上为待审核建议；批准前不创建服务模块、不接入 lifespan、不调用 `run_tick()`。所有关键写入仍以现有 SQLite lease/fencing 为跨进程权威，进程内锁不能替代。

## 2026-10-13：C-C-2c-0 设计文档按 P0 证据核对报告修订

### 决策

基于只读 P0 证据核对报告，修订 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md`，形成待批准版本。四项 P0 均维持接入前阻断；#19 并入 P0-4 不新增独立阻断编号；#15 维持「强烈建议，不独立阻断」。

### 理由

修订目标是把「已由人工确认的方向」与「尚待裁定的参数」分开，把「源码事实 / 已有测试证明 / 推断 / 待验证」分开，避免把建议或未来计划写成已实现事实。具体：

- P0-1（测试 #7）：现有 `test_stale_guard_*` 仅覆盖同连接/单函数拒绝，不能代替「跨连接/OS 子进程接管 + 完整 run_tick 流程」的端到端用例。
- P0-2（heartbeat）：`_heartbeat_if_needed()` 是未接入 `run_tick()` 的孤立函数，必须先接入有效调用路径或明确删除/重构。
- P0-3（shutdown）：`ThreadPoolExecutor.shutdown(wait=False)` 不保证进程在后台线程运行时退出；daemon 线程、线程池内部线程与 Uvicorn 关闭不能混为一谈；最终方案与宽限数值待裁定。
- P0-4（run_key/catch-up）：`_new_run_key` 只有 manual 前缀；桶粒度、时区边界、同桶重试、recovery 键关系待裁定，禁止自行设定 ISO 分钟槽等具体值。

### 边界

本次修订只改设计文档与交接说明（`docs/current-task.md`、本决策记录），未修改任何 `app/` 生产代码或测试代码，未运行测试或调度，未连接真实数据库。仍处于等待人工批准状态，不进入 C-C-2c-1。

## 待决策

（与设计文档第 13 章一致，供人工/后续批次决策）

P0 级（进入 C-C-2c-1 前必须书面裁定）——**2026-10-14 状态：P0-1 已接受、P0-2/P0-3 已追认（见文末 2026-10-14 条目），P0-4 待方案裁定：**

- ~~P0-2~~ 已于 2026-10-14 追认现有实现参数（见文末）。
- ~~P0-3~~ 已于 2026-10-14 有条件追认方案 C 与 35 秒暂定值（见文末）；进程退出独立验证与参数校准仍为未决测试任务。
- P0-4 自动 run_key 具体桶粒度、时区/时间边界、同桶重试语义、`scheduled`/`recovery` 键关系；**注意：`scheduled:{epoch//poll}` 桶键已在 C-C-2c 实现中落地（见本文件 C-C-2c 条目），但桶语义、失败重试规则与 recovery 键关系仍未裁定。** 禁止在裁定前把任何候选方案写成已批准。
- ~~P0-1~~ 已于 2026-10-14 正式接受（范围仅限 fencing 测试批次 7 passed）。

P1 级（相应批次前裁定）：

- 自动 tick 默认开启还是默认关闭（建议默认关闭或显式环境变量启用，首次启用仅限本地 loopback）。
- 基础轮询间隔（建议 60s，下限 30s）与 lease busy 抖动参数。
- on_event 迁移到 lifespan 的批次归属。
- run 历史保留周期与清理方式。
- 是否/何时引入通知投递及其幂等键。
- 测试矩阵 #16（自动与手动 tick 竞争）在提供任何手动 tick HTTP 入口前必须完成。

## 2026-10-13：全项目工程化治理保留扁平领域模块结构

### 决策

- 不机械搬迁为 `api/`、`services/`、`repositories/` 等完整分层目录。
- 保留 `app/main.py` 作为 FastAPI 组装和路由入口，但承认其体积偏大；后续按领域小步提取。
- 已经独立且测试充分的领域模块继续放在 `app/` 根下，通过 `AGENTS.md` 和模块职责文档约束依赖方向。

### 否决方案

- 一次性把 80+ 路由和 schema 全部分目录：迁移面过大，容易改变导入、事务和测试语义。
- 新增空接口、工厂或基类：没有真实多实现需求，会增加维护成本。

### 理由

当前领域模块边界已经基本清楚，最高风险不是目录形式，而是 `main.py` 职责混杂和自动调度生命周期尚未定稿。治理应优先提升安全、测试隔离和可恢复性。

## 2026-10-13：集中 OpenAI 兼容模型配置

### 决策

新增 `app/model_config.py`，由 `load_model_config()` 在调用时读取 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；聊天、记忆提取、画像推测和建议模型润色共用该配置。

### 理由与边界

- 消除 4 处重复默认值和环境读取逻辑。
- 不在导入时缓存 Key，保证测试 monkeypatch 和运行时环境变更有效。
- 该模块只保存配置，不发 HTTP 请求、不记录密钥。
- 建议模型仍必须同时满足 `RECOMMENDATIONS_USE_MODEL=1` 和存在 Key，默认继续关闭。

## 2026-10-13：测试默认强制隔离真实数据库和密钥

### 决策

新增 `tests/conftest.py`，在 pytest 配置阶段把 `DATABASE_PATH` 重定向到系统 Temp，并设置临时 `PAOS_SECRET_KEY_PATH`、清空继承的 `LLM_API_KEY` / `PAOS_SECRET_KEY`；每个测试自动校验当前数据库不是真实开发库。

### 否决方案

只依赖各测试文件自觉设置临时库。历史上已出现 pytest 导入顺序可能让模块级全局指向默认库的问题，单点守卫更安全。

### 逃生口

仅在用户明确授权时设置 `PAOS_TEST_ALLOW_REAL_DATABASE=1`；常规开发和 CI 不使用。

## 2026-10-13：移除 run.bat 明文密钥并改为保守启动器

### 决策

- 删除 `run.bat` 中的明文 LLM Key、硬编码模型和 base URL。
- 脚本基于 `%~dp0` 定位项目根目录。
- 只在外部环境未定义变量时加载可选 `.env`。
- 使用 `APP_HOST` / `APP_PORT`，默认 `127.0.0.1:8000`。
- `APP_HOST` 仅允许 `127.0.0.1`、`localhost`、`::1`，拒绝非 loopback 值。
- 检查虚拟环境解释器和端口监听，透传 Uvicorn 退出码。

### 明确不做

不自动安装依赖、不执行数据库初始化脚本、不运行提醒 tick、不写入密钥。

## 2026-10-13：SQLite HTTP 连接增加 busy timeout，不切换日志模式

### 决策

`app.main.db()` 使用 `sqlite3.connect(DB_PATH, timeout=5.0)` 并设置 `PRAGMA busy_timeout=5000`。

### 否决方案

本批不切换 WAL，不修改 journal mode，不调整真实库 pragma。当前目标只是减少短暂 SQLITE_BUSY，数据库文件行为和备份方式保持不变。

## 2026-10-13：暂不迁移 FastAPI lifespan

### 决策

保留现有 `@app.on_event("startup"/"shutdown")` 和 ConnectorScheduler 生命周期。

### 理由

单独迁移 lifespan 只消除警告但无业务收益；未来提醒自动调度必须重新设计 lifespan、执行器和关闭超时，适合在 C-C-2c 实现批次一并完成并专项测试。

## 2026-10-13：提醒自动调度继续保持关闭

### 决策

C-C-2c-0 设计文档已创建，但自动提醒后台循环仍不接入 startup/shutdown。

### 理由

设计仍需人工审核；长 tick heartbeat、shutdown 超时、busy/lost/database_error、多 worker/热重载和真实 OS 并发测试必须先确认。现有 lease/fencing 是调度核心能力，不等于自动调度已经完成。

## 2026-10-13：C-C-2c 自动提醒默认关闭并接入 lifespan

### 决策

- 新增 `app/reminder_scheduler_service.py`，由 FastAPI lifespan 管理；默认 `REMINDER_SCHEDULER_ENABLED=0`。
- asyncio loop 只负责定时、状态和关闭；同步 SQLite `run_tick()` 在专用 daemon 单线程 worker 中执行。
- 自动 tick 创建独立 sqlite3 连接，设置 foreign keys 和 busy timeout，不与 HTTP 请求连接共享。
- 跨进程互斥继续以 SQLite lease/fencing 为权威；进程内锁和单 worker 只防同进程重叠。
- 配置固定为：
  - `REMINDER_SCHEDULER_ENABLED`，默认关闭；
  - `REMINDER_SCHEDULER_POLL_SECONDS`，默认 60，环境配置下限 30 秒；
  - `REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS`，默认 35，允许 1..300 秒。
- heartbeat 间隔固定 10 秒，tick 软上限固定 90 秒；仅通过测试接缝调整，不新增生产环境表面。
- scheduled run key 使用 `scheduled:<按 poll interval 归一化的时间桶>`；同桶幂等，不承诺补跑所有历史周期。
- shutdown 超时不强杀同步线程；服务进入 `stopped_with_active_tick`，依赖 lease TTL、fencing 和 5 分钟 stale recovery。
- `/api/health` 保留 `status`、`mode`，新增嵌套 `reminder_scheduler` 状态对象。


### 收尾补充决策

- scheduler 配置解析前移到 `init_db()` 之前：非法配置属于快速失败，不应先触发幂等建表/迁移，也不应留下 ConnectorScheduler。
- tick 耗时和结果由 `ReminderSchedulerService` 记录；lease acquire 使用 info，常规 heartbeat/release 使用 debug，异常或 owner 变化使用 warning/error，避免正常 busy 场景形成日志风暴。
- lifespan 对 service 构造阶段也使用统一 finally 回滚 ConnectorScheduler，而不是只覆盖 `start()` 之后的异常。

### 否决方案

- 默认开启：本地应用可能在多 worker/reload/开发者不知情时竞争 tick，不符合最小副作用原则。
- 在 asyncio 中直接调用同步 sqlite3：可能阻塞事件循环和 ConnectorScheduler。
- 新增独立 heartbeat 线程共享同一 SQLite 连接：存在连接/事务跨线程和写锁竞争风险。
- 取消 asyncio Future 时强制终止线程：Python 不提供安全终止任意 SQLite 调用的机制，可能破坏事务/连接状态。
- 把 busy 当作错误高速重试：多 worker 中 busy 是正常结果，会造成日志风暴和不必要的 SQLite 压力。
- 在本批加入通知发送器：当前没有通知侧幂等/重试模型，外部副作用不能与 SQLite 片段提交伪装成原子或 exactly-once。

### 结果

C-C-2c 已实现并通过 445 passed、1 skipped、1 warning；真实开发库未变化。



## 2026-10-13：C-C-2d 使用事务性 outbox 与 fake-test 消费器

### 决策

- 通知事件和渠道投递分成 `reminder_notification_events` 与 `reminder_notification_deliveries` 两张表。
- reminder `scheduled/snoozed -> due`、event、pending delivery 在同一个 SQLite guarded transaction 提交；渠道投递由独立服务异步处理。
- 幂等键使用 reminder id + 业务时机（首次 `scheduled_at`，每次 snooze 使用 `snoozed_until`），不使用随机 UUID 去重。
- 当前只实现 `fake_test` 渠道；服务白名单拒绝任何非 fake channel，不提供 webhook/import loader。
- 消费器默认关闭（`NOTIFICATION_DELIVERY_ENABLED=0`），同步 SQLite/channel 调用在 daemon 单线程执行，asyncio 只负责定时。
- 多进程领取权威是 SQLite 条件 UPDATE；重试为确定性指数退避，无 jitter；上限 5 次、初始 30 秒、最大 3600 秒。
- 投递语义明确为 at-least-once；stale running 恢复可能在未来真实渠道中造成重复投递。

### 否决方案

- 否决单表把业务事件和渠道生命周期混在一起：未来多渠道和死信诊断不清晰。
- 否决内存队列：进程退出会丢失待通知意图，且无法跨进程恢复。
- 否决真实 Webhook/邮件渠道：本任务明确禁止外部副作用，凭据和安全模型需另行设计。
- 否决 exactly-once 承诺：无法证明外部渠道在“已发送但本地未确认”崩溃时可去重。

## 2026-10-14：第一轮书面裁定登记（R1/R3、P0-1、C-C-2d、P0-2/P0-3 追认）

以下为用户书面裁定的登记（本批次未修改任何代码/测试，未写真实库，未启动任何服务）。

### 真实数据库（R1/R3）

- 保留 `data/personal_ai_os.db` 当前版本（352,256 B / 2026-10-13 19:49:11.445 / SHA-256 `49F1...F0D8`）；不覆盖、不恢复、不删除当前库及两份备份。
- 19:49:11 写入来源标记为「无法追溯」，不再扩大搜索；该问题保留为历史风险，**不描述为已解决**。
- 继续禁止真实库写入、初始化、迁移和应用启动；R3（通知表建表）本次不授权，也不授权通过启动应用触发 `init_db()`。
- 保留当前库不代表对其业务数据/结构完整验收。

### P0-1 正式接受

- 接受范围仅限 fencing 测试批次交付（`tests/test_reminder_scheduler_fencing.py` 7 passed，2026-10-13 实跑）。
- 不代表 C-C-2c 全部验收、P0-2/3/4 已解决、调度器可在真实库启动、或真实库风险消除。

### C-C-2d 有条件验收

- 依据 m00679 独立审查与既有隔离测试（33 专项 + 全量 480 passed，2026-10-13）验收。
- 验收范围：fake 渠道、事件/投递持久化、outbox 事务边界、幂等/claim/重试/死信/stale 恢复、默认关闭设计。
- 六项限制：真实库通知表未建；无真实渠道；无死信重放管理；无端到端真实投递；数据库安全约束不解除；报告冲突或发现未披露 P0 缺陷时暂停验收并报告。

### P0-2 追认现有 heartbeat 参数

- heartbeat 间隔 10 秒；lease TTL 30 秒；tick 上下文边界检查续租；heartbeat 失败不伪装成功。
- 追认的是当前已实现参数与策略，不代表所有异常场景已充分验证。

### P0-3 有条件追认方案 C 与 35 秒暂定值

- 追认自建 daemon 单线程 worker、有界等待/有界 join、超时进入 `stopped_with_active_tick` 等状态、不谎报完全停止。
- `DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 35.0` 为**暂定**运行参数，未完成实测校准，不得标记为已校准。
- 未决事项（后续测试任务，本次未修改实现）：①35 秒宽限值是否匹配实际任务耗时；②进程最终能否退出的独立验证；③不强制杀线程与保证进程退出的取舍；④关闭超时后正在执行的同步 SQLite 操作的状态风险。

### P0-4 状态澄清（重要事实更正）

- **P0-4 并非完全未实现**：C-C-2c 实现批次已落地 `scheduled:{epoch 秒 // poll 间隔}` 桶键（`app/reminder_scheduler_service.py` `_submit_tick` 与 `run_scheduled_tick` 两处生成），`docs/decisions.md` C-C-2c 条目亦有记载；此前交接文档/设计文档中「只有 manual 前缀」的描述为实现前旧状态，已滞后。
- 同桶语义现状：`run_tick` 对已存在的 run_key（无论 succeeded/failed/running/abandoned）直接返回既有记录；桶寿命 = poll 间隔（默认 60 秒），失败桶最多延迟一个 poll 周期后在下一桶自然重试。
- 仍待裁定：桶粒度是否维持 poll 间隔、时区/对齐规则、同桶失败重试规则、`recovery` 命名空间是否引入、停机后是否补跑、`tick_once` 默认 run_key 语义。
- 2026-10-14 只读研究产出候选方案 A/B 对比与推荐（会话内报告），**均未获批准，不构成技术决策**。

### P1 级待决策（延续）

- 基础轮询间隔默认 60 秒已实现；busy 抖动参数、run/notification 历史保留策略、死信重放等仍待后续批次。
