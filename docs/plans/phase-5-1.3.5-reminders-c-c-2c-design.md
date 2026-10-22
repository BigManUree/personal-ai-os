# Personal AI OS 1.3.5-C-C-2c 自动调度生命周期与长任务安全性设计

创建时间：2026-10-13 15:29:52 CST  
阶段：C-C-2c-0 只读设计审查  
状态：设计已批准并由 C-C-2c 实施；默认关闭。实施结果见第 16 章  
最近修订：2026-10-13 基于 P0 证据核对报告修订（P0-1/2/3/4 维持接入前阻断；#19 并入 P0-4；#15 维持强烈建议不升级阻断）

## 0. 审查范围与结论

本设计基于当前工作区源码静态审查，不修改生产代码、测试代码、配置或数据库 schema，不运行 `run_tick()`，不启动后台任务。

**推荐结论：** 后续实现应新增独立的 `ReminderSchedulerService`（名称可在实现批次微调），由 FastAPI lifespan 管理一个 asyncio 控制循环；循环本身只负责定时唤醒、租约竞争结果处理和状态观测，实际同步 SQLite `run_tick()` 必须在专用单线程执行器中运行。租约仍以现有 SQLite lease 表和 fencing token 为跨进程权威，不能以 asyncio Lock、进程内线程锁或启动标志替代。

自动调度应继续保持默认关闭或显式启用，并且在多 worker / 热重载场景下允许所有进程启动循环，但只有当前有效租约持有者执行 `run_tick()`。长 tick 优先采用“候选项处理边界显式续租 + 同步执行器串行化”的保守方案；本阶段不推荐独立 heartbeat 线程共享现有 SQLite 连接。

## 1. 当前实现与生命周期入口

### 1.1 FastAPI 生命周期现状（源码事实）

- `app/main.py:796` 使用 `@app.on_event("startup")`，`startup()` 中先调用 `init_db()`。
- `app/main.py:799-803` 创建并启动全局 `ConnectorScheduler`：
  - 模块级变量：`app/main.py:2964` `connector_scheduler_instance`；
  - 若实例为 `None` 则创建；
  - 每次 startup 都调用 `connector_scheduler_instance.start()`。
- `app/main.py:806-811` 使用 `@app.on_event("shutdown")`，异步调用 `ConnectorScheduler.stop()` 并置空实例。
- 当前没有 reminder scheduler startup/shutdown 接入；静态搜索只发现 run 表 schema / migration，没有 `run_tick()` 生命周期调用。
- `on_event` 已产生 FastAPI deprecation warning，但本设计不强制在本阶段顺手迁移全部生命周期，避免无关重构。

### 1.2 ConnectorScheduler 可复用与不可复用部分（源码事实）

`app/connector_scheduler.py`：

- `ConnectorScheduler.start()`（104-109）在 task 已存在且未完成时幂等返回，否则创建 `asyncio.create_task(self._loop(), name="connector-scheduler")`。
- `stop()`（110-119）设置 stopping、cancel task、await task，并吞掉 `CancelledError` 和普通异常；目前没有显式关闭超时。
- `_loop()`（125-138）捕获 tick 异常以保持循环存活，并用 `asyncio.wait_for(self._sleep(poll), timeout=poll+1)` 等待。
- `tick()`（140-166）使用进程内 `asyncio.Lock`，若锁已占用直接返回空结果；任务按数据源串行执行。
- 文件头注释（3-9）明确其设计边界是 local single-user、single uvicorn process，无 distributed lock。

可借鉴：幂等 start/stop、task 命名、stopping event、进程内防重入、循环异常隔离。  
不可照搬：把进程内 asyncio Lock 当作多 worker 互斥；`stop()` 无超时等待同步阻塞操作的假设。

### 1.3 数据库连接事实

- `app/main.py:33`：`DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "personal_ai_os.db")))`。
- `app/main.py:118-128`：`db()` 每次创建一个 `sqlite3.connect(DB_PATH, timeout=5.0)`，设置 `sqlite3.Row`、`PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=5000`，上下文正常退出时 commit，finally close。
- `run_tick()` 要求连接开启 foreign keys（`app/reminder_scheduler.py:692-693`）。
- `run_tick()` 在发现 `conn.in_transaction` 时会直接 `conn.commit()`（698-699），这是 C-C-2c 必须重新评估的调用方事务风险（见第 9 章）。

## 2. 当前安全边界

### 2.1 已由 C-C-2b 实现（源码事实）

- 跨进程租约由 `app/reminder_scheduler_lock.py` 提供：
  - `acquire_lease()`（209）、`heartbeat_lease()`（284）、`release_lease()`（328）；
  - 默认 TTL 30 秒（`DEFAULT_TTL_SECONDS=30.0`）；
  - fencing token 在接管空闲/过期租约时递增；
  - heartbeat SQL 要求 owner/lease/fencing 完全匹配且 `expires_at > now`；
  - release SQL 要求 owner/lease/fencing 完全匹配且未释放。
- `run_tick()`（670-775）在业务扫描前获取租约：
  - busy 抛 `LeaseBusyError`；
  - 数据库错误抛 `LeaseDatabaseError`；
  - 租约失效抛 `LeaseLostError`；
  - finally 用完整身份条件释放租约。
- guarded write：
  - `_begin_guarded_write()`（203-220）开启 `BEGIN IMMEDIATE` 并校验 lease；
  - `_commit_guarded_write()`（223-238）提交前在同一写事务内再次校验；
  - `guarded_write()`（241-252）异常回滚并保留原异常。
- 受保护路径包括：
  - `recover_stale_runs(..., guard=...)`（141-157）；
  - `_create_run(..., guard=...)`（265 起）；
  - `_finish_run(..., guard=...)`（297 起）；
  - `create_reminder_guarded()`（343）；
  - `transition_reminder_guarded()`（335）；
  - `expire_reminders_guarded()`（328）。

### 2.2 已验证性质（测试事实，历史结果）

以下均为 C-C-2b 的历史执行结果（引用自 `docs/plans/phase-5-1.3.5-reminders-review.md` 第 18 章），不是 C-C-2c-0 本轮执行：

- fencing 专项：6 passed；
- 提醒完整专项：90 passed；
- 全量：409 passed、1 skipped、5 warnings；
- compileall 退出码 0。

### 2.2.1 P0 现状与证据边界（本轮只读核实）

> **2026-10-14 状态更新（裁定登记批次，只读核对）：**
> - **P0-1**：fencing 测试批次已实现并 7 passed（2026-10-13 实跑）；用户已正式接受（范围仅限该批次）。
> - **P0-2**：已随 C-C-2c 实现落地——`_heartbeat_if_needed` 已接入 `run_tick()` 的 tick 上下文边界检查（当前代码 `app/reminder_scheduler.py` `_TickControl`），heartbeat 间隔 10 秒、TTL 30 秒；用户已于 2026-10-14 追认该参数。下述「孤立函数」为实现前旧状态。
> - **P0-3**：已随 C-C-2c 按方案 C 实现（自建 daemon 单线程 worker、有界 join、超时进入 `stopped_with_active_tick`、`shutdown_timed_out` 标志；35 秒暂定值）；用户已于 2026-10-14 有条件追认。未决：35 秒实测校准、进程退出独立验证。下述「无 ReminderSchedulerService」为实现前旧状态。
> - **P0-4**：部分实现——`scheduled:{epoch 秒 // poll 间隔}` 桶键已在 `app/reminder_scheduler_service.py` 落地（见 8.3 节 2026-10-14 更新）；用户尚未裁定桶语义/失败重试/`recovery` 键/补跑。下述「只有 manual 前缀」为实现前旧状态。

以下四项 P0 已定位为「接入前阻断」，本节区分**已被现有测试证明**与**尚未被证明**，不得把后者描述为已验证。相应验收条件见第 11/14 章。

**P0-1：接管后旧 owner 的端到端 fencing 验证（测试矩阵 #7）**

- 已证明（`tests/test_reminder_scheduler_fencing.py`，只读查看，未执行）：
  - `test_stale_guard_cannot_create_reminder_after_takeover`（120-133）：**同一连接**内先获取旧租约（ttl=1），再同一连接获取新租约（fencing+1），随后用旧 guard 调用 `create_reminder_guarded()` 被 `LeaseLostError` 拒绝，且 dedup 计数为 0。
  - `test_stale_guard_cannot_transition_or_expire_reminder_after_takeover`（136-153）：同上模式，覆盖 `transition_reminder_guarded()`，最终状态仍为 `scheduled`。
  - `test_stale_guard_cannot_finish_run_owned_by_older_fencing_token`（156-176）：旧租约 + `_create_run` 后新租约接管，旧 guard 调 `_finish_run()` 被拒绝，run 仍为 running/old owner/old fencing。
  - `test_two_independent_connections_competing_for_tick_have_one_run_and_release`（179-217）：双连接 barrier 竞争，恰好一个 executed、一个 busy，最终单 run、租约释放。
- 尚未证明：
  - 上述三个 stale_guard 测试都是「同一连接二次 acquire 接管」，**不是**跨连接或跨进程接管。
  - **没有**「旧 owner 获取租约 → 暂停 → 租约过期 → 新 owner 接管 → 旧 owner 恢复 → 旧 owner 继续走完整 `run_tick()` 主流程尝试写入 → 最终数据库状态断言」的端到端用例。
- 结论：测试矩阵 #7 必须新增独立连接 / OS 子进程用例，覆盖上述完整顺序，且必须使用独立临时 SQLite 文件（沿用 `tests/conftest.py` 保护）。当前不得声称该用例已实现或已执行。

**P0-2：heartbeat 有效调用策略**

- 源码事实：`_heartbeat_if_needed()`（`app/reminder_scheduler.py:255-262`）已定义但 `run_tick()`（670-775）主流程无任何调用点；grep 全工程确认它是孤立函数。这正是第 14 章阻断条件 #2 点名的「函数定义了 heartbeat 但主流程不调用」落空路径。
- 已证明：`tests/test_reminder_scheduler_lock.py` 的 `test_heartbeat_requires_exact_owner_lease_and_fencing_token`（93-109）只验证 `heartbeat_lease` 身份匹配语义，**不**证明 `run_tick` 长 tick 会续租。
- 结论：见第 4.1 节的硬要求；在实现前不得把 heartbeat 触达点参数写成已批准。

**P0-3：shutdown 超时与线程/进程退出策略**

- 源码事实：当前无 `ReminderSchedulerService`、无 `ThreadPoolExecutor`；`app/connector_scheduler.py:stop()`（110-119）只 `stopping.set()` + `task.cancel()` + `await`，**无任何关闭超时**，不能作为 reminder service 的关闭模板。`app/main.py:806-811` shutdown 只处理 connector。
- 结论：见第 6.2 节的语义区分与待裁定项；在完成技术可行性分析前不选定最终方案。

**P0-4：自动 run_key 与 catch-up 语义**

- 源码事实：`_new_run_key(now)`（120-122）只生成 `manual:{iso}:{uuid}`，**没有任何 scheduled/recovery 前缀或时间桶生成逻辑**；`run_tick` 的 `run_key` 查重在 700-703，`run_key UNIQUE` 在 `app/main.py` schema。
- 已证明（业务窗口层，只读）：`tests/test_reminder_scheduler_recovery.py` 的 `test_daily_review_catchup_boundary_creates_at_two_hours_and_skips_after`（45）与 `test_weekly_review_uses_24_hour_catchup_and_does_not_backfill_old_weeks`（66）证明 `review_candidate()` 的半开窗口 `[scheduled, expiry)` 语义（daily 2 小时、weekly 24 小时），**不是**证明 service 层停机重启集成正确。
- 结论：见第 8.3 节；不得自行设定时间桶粒度或回溯窗口。

### 2.3 尚未保证（设计缺口）

- 没有自动调度循环；没有 FastAPI 生命周期接入。
- 没有长 tick heartbeat 策略；当前 `_heartbeat_if_needed()`（255-262）只在显式调用时续租，`run_tick()` 主流程未调用该函数（孤立函数）。
- guarded write 的每次事务能证明旧 fencing 不能在接管后提交新事务，但不能自动回滚接管前已提交的业务数据。
- 自动调度和手动 tick 的上层重试/返回策略尚未定义。
- 没有自动调度日志、指标、run 保留清理。
- `run_tick()` 自动提交调用方遗留事务的行为对自动调度连接可控，但对复用外部连接存在语义风险。
- shutdown 超时后的线程/进程退出策略未定（见 6.2）。
- 自动 `run_key` 的时间桶粒度、catch-up 窗口与 `scheduled`/`recovery` 键关系未定（见 8.3）。

## 3. 多进程运行模型

### 3.1 支持场景

实现批次应明确支持：

- Uvicorn 单 worker；
- 同一台机器上多个应用实例 / 多 worker；
- 开发热重载产生的父子进程；
- 进程重启后接管过期租约；
- 同一数据库文件上的手动 tick 与自动 tick。

不承诺：

- 多机共享 SQLite 数据库；
- 公网多用户部署；
- exactly-once；
- 任意长事务或无限时长 tick。

### 3.2 进程角色

每个应用进程均可启动一个轻量 reminder scheduler loop，但 loop 不等价于拥有调度权：

1. loop 到点后进入串行执行器；
2. 使用进程内状态防止同进程重叠；
3. 创建独立 SQLite 连接调用 `run_tick(trigger_type="scheduled")`；
4. `run_tick()` 通过 SQLite lease 决定是否实际执行业务；
5. busy 是正常结果：记录 debug/info 日志，跳过本轮，不创建 run；
6. database_error 按可重试错误退避；
7. lease_lost 停止当前 tick，已提交部分保留，后续依赖 recovery 和 dedup 解释。

### 3.3 重试与退避建议

- 基础唤醒间隔：建议 60 秒，具体应与提醒规则最小粒度匹配；实现时可配置但必须设置最小值（例如 30 秒）。
- 获取租约 busy：不立即重试，等待下一周期；日志级别 debug/info，避免多 worker 刷屏。
- database_error / locked：短退避，例如 2s、5s、15s，加 ±20% 抖动，最多有限次数；恢复后回到正常周期。
- 连续失败：保留失败计数和最后错误；不影响 Web 应用启动成功，但应能从日志和 run 表观察。
- 热重载：父子进程可能同时启动循环，依赖 lease 保证单执行者；进程退出应尽量释放租约，异常死亡则等待 TTL/stale recovery。

## 4. Heartbeat / 续租方案对比

| 方案 | 结论 | 优点 | 风险 |
|---|---|---|---|
| A. 候选项/阶段边界显式续租 | **首选** | 不跨线程共享连接；续租点清晰；易在同步代码中测试；不改变 fencing 模型 | 单个候选/单阶段若阻塞超过 TTL，后续写入才发现 lease_lost |
| B. 独立 heartbeat 线程 | 暂缓 | 理论上可更频繁续租 | SQLite 连接跨线程共享风险、事务竞争、关闭顺序复杂、需要线程间 lost 事件 |
| C. 独立 asyncio heartbeat 任务 | 不推荐直接用于同步连接 | 与 asyncio 生命周期一致 | `run_tick()` 是同步 sqlite3；若另开连接续租，需处理双连接写锁和身份状态传播 |
| D. 限制 tick 最大执行时间 | 必须作为补充 | 避免无限长任务；shutdown 行为可预测 | 不能安全中断任意同步 SQLite 操作；超时只能阻止后续写入并等待/放弃线程 |

### 4.1 推荐方案与 P0-2 硬要求

采用 **A + D**，且 P0-2 维持接入前阻断。以下「硬要求」是批准后实现必须满足的语义，不属于「建议」；但触发条件与具体数值仍待裁定，见文末。

**硬要求（P0-2）：**

- heartbeat 必须在已批准的调度执行路径中被真正调用；`run_tick()` 主流程当前没有调用 `_heartbeat_if_needed()`，实现时要么把它接入有效调用路径，要么在实现前明确删除/重构并解释替代方案，禁止保留「已定义但主流程不调用」的落空路径。
- 明确 heartbeat 的调用触发条件与失败处理：heartbeat 返回 `lost` 后立即停止启动新的候选 / 新事务；返回 `database_error` 按 fatal tick 错误处理，不伪装成功。
- heartbeat 失败后禁止任何新的未受保护业务写入；heartbeat 是「延长仍有效的同一 owner/lease/fencing」，**不是** fencing 的替代品——所有业务写路径仍必须依赖 `guarded_write`（BEGIN IMMEDIATE + COMMIT 前身份复核）。
- 受保护与不受保护的现有写入入口必须分别列明（见 9.2）；实现批次新增任何非 guard 写入口必须在文档与测试中单独说明。

**推荐做法（非硬性数值）：**

- `run_tick()` 仍在专用单线程 executor 中同步执行；自动调度使用短 tick，禁止常驻长事务。
- 候选阶段边界（recovery 后创建 run 前、每处理若干候选后、过期/候选生成/due 推进之间、候选批次边界）作为 heartbeat 候选触达点；具体「每 N 个候选」还是「固定秒数」尚未裁定，不得在此写成已批准参数。
- 设置 tick 软上限（超过后不再开始新工作）；软上限倍数与 heartbeat 间隔的**具体数值待裁定**，只要求续租间隔明显小于 30 秒 TTL（见 9.2 的 heartbeat 语义边界）。

### 4.2 为什么不首选独立 heartbeat 线程

SQLite 连接、事务和 SAVEPOINT 不应在未证明线程安全的情况下被 heartbeat 与业务线程共享。若 heartbeat 使用独立连接，则可能遇到：

- heartbeat `BEGIN IMMEDIATE` 与业务写事务竞争；
- heartbeat 成功但主业务连接已经进入未知状态；
- shutdown 时 heartbeat 晚于业务线程释放/接管；
- 需要额外 Event/Queue 传播 lost，且无法中断正在执行的同步 SQL。

除非后续证明 tick 中存在不可拆分的长时间阻塞阶段，否则不引入。

## 5. 推荐自动调度架构

建议新增服务模块（实现阶段，不属于本设计批）：

- `app/reminder_scheduler_service.py`（建议）：
  - `ReminderSchedulerService`；
  - asyncio `_loop()`；
  - 进程内 `asyncio.Lock` 或 running 标志；
  - 专用 `ThreadPoolExecutor(max_workers=1)`；
  - 独立 sqlite 连接工厂；
  - 统一日志和退避；
  - `start()` / `stop()` / `is_running()` / `trigger_once()`（手动触发是否暴露由后续 API 批决定）。

`app/main.py`：

- 将现有 `startup`/`shutdown` 迁移或包装为 FastAPI lifespan 更佳，但为最小变更也可先保留 `on_event`；若保留，应保证同一全局服务实例幂等启动/停止。
- startup：`init_db()` 后实例化并按需启动 reminder service；是否启用应由配置决定，默认必须安全（建议默认关闭或仅本地显式开启）。
- shutdown：先停止 reminder loop，再停止 connector scheduler 或按资源依赖明确顺序；二者不共享租约。

执行模型：

```text
asyncio loop
  -> process-local gate
  -> executor.submit(_run_scheduled_tick)
  -> worker opens isolated sqlite connection
  -> run_tick(..., trigger_type="scheduled")
  -> connection closes
  -> result/future returns to asyncio loop for logging
```

同步 `run_tick()` 不被多个 asyncio task 共享同一个 connection。

## 6. startup/shutdown 状态机

### 6.1 服务状态

建议服务内部使用显式状态：`stopped -> starting -> running -> stopping -> stopped`，另保留 `failed` 仅表示启动资源初始化失败，不表示业务 tick 失败。

幂等要求：

- `start()` 在 `running` 时直接返回；`starting` 时等待或返回同一启动结果，不创建第二个循环。
- `stop()` 在 `stopped/stopping` 时直接返回，重复调用不抛异常。
- startup 中途失败：释放已创建 executor、标记 stopped、记录异常；不留下可继续提交 tick 的半初始化对象。
- shutdown 与 startup 并发：以状态转换和 asyncio 锁串行化，禁止出现两个 loop。

### 6.2 shutdown 与正在运行的 tick（P0-3）

> **2026-10-14 状态更新：方案 C 已随 C-C-2c 实现并被用户有条件追认（35 秒为暂定值，未实测校准；进程退出独立验证未完成，列为后续测试任务）。下文选项表保留为实现前论证记录。**

维持接入前阻断。已认可的设计方向：有限等待、通知任务停止、不无限阻塞应用关闭、超时后不声称正在运行的同步调用已被取消、记录 `shutdown_timeout` 等诊断信息、设计并验证明确的退出策略。

**必须区分的四个概念（技术可行性约束）：**

1. **停止请求**：`stop()` 被调用，服务发出停止信号（如 `asyncio.Event`）。
2. **停止等待超时**：`stop()` 等待正在运行的 tick future 的宽限时间到期（建议配置值，默认候选 35 秒，但**未经实测校准，仍是待裁定参数**）。
3. **线程实际退出**：`ThreadPoolExecutor` 的工作线程是否结束，由 `shutdown(wait=...)` 决定；`task.cancel()` **不能**中断线程池中正在执行的同步 `sqlite3` 调用。
4. **进程退出**：解释器是否真正退出，与工作线程是否为 daemon 无关；`ThreadPoolExecutor` 的内部工作线程不因把某个业务线程标记为 daemon 而自动变为可中断。

**必须澄清的错误混同（不得写入实现假设）：**

- `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)` 只保证「不再接受新任务、未开始的 future 被取消」，**不保证** Python 进程能在后台工作线程仍运行时退出；`wait=False` 不阻塞主线程，但工作线程本身是非 daemon 的，解释器关闭时仍会等待它们（Python 的线程池线程不是 daemon）。
- 单独设置 daemon worker（若未来自建线程）不能让 `ThreadPoolExecutor` 的进程退出行为满足预期；两者是不同机制，不能混为一谈。
- 不得依赖尚未证明的 recovery 机制来掩盖进程退出风险——`recover_stale_runs()` 能收敛「已存在的 running run」，但它不负责「进程是否真的能退出」。

**可选方案（待裁定，禁止在实现前选定）：**

| 方案 | 行为差异 | 风险 | 验证要求 |
|---|---|---|---|
| A. 有界等待 + `shutdown(wait=True)` | 宽限内等待完成；超时后仍等待线程结束才返回 | 若 tick 长阻塞，`stop()`/Uvicorn 关闭可能长时间挂起 | 需证明 tick 不会超过宽限（阶段边界检查 + 软上限） |
| B. 有界等待 + `shutdown(wait=False, cancel_futures=True)` | 超时后立即返回，不等待线程 | 工作线程仍非 daemon，解释器关闭仍可能被线程拖住；`shutdown_timeout` 只是「不再等」，不等于「进程可退出」 | 需实测 Uvicorn 关闭耗时与线程残留 |
| C. 自建 daemon 单线程 worker + 显式 join 宽限 | 进程可退出；join 有界 | 需要自行实现任务队列/结果回传，复杂度高；daemon 线程在进程退出时被强制终止，正在写的同步 SQLite 状态未定义 | 需证明无残留写事务 + TTL/recovery 能收敛 |

当前资料不足以确定最终方案，保留为待裁定决策。无论选哪个方案，接入前验收必须证明「不会无限挂起 Uvicorn 关闭」，并记录 `shutdown_timeout`；「不杀线程」与「进程最终能退出」是两个独立断言，必须分别验证。

### 6.3 与 ConnectorScheduler 协调

两者使用不同资源：connector 是外部数据源同步，reminder 是本地调度写入；不共享 asyncio Lock 或 lease。

建议关闭顺序：先停止 reminder service（避免应用关闭期间继续产生本地提醒状态变化），再停止 connector scheduler；启动顺序保持现有 connector 行为，reminder 在其后或同一点启动均可，但二者都必须幂等。实现批次应避免让 reminder 关闭等待阻塞 connector 关闭，需分别设置有界超时。

测试环境必须显式关闭服务，避免 TestClient 反复创建实例造成后台任务泄漏。

## 7. lease busy、lease lost、database_error 处理

| 情况 | 自动 tick 行为 | run 记录 | 日志 | 退避 |
|---|---|---|---|---|
| acquired | 执行 tick | 创建 scheduled/recovery run | info/debug 含 run_id、fencing | 正常周期 |
| busy | 跳过，不扫描不写入 | 不创建 run | debug/info，不刷屏 | 下一周期 |
| lease_lost | 中止新写入，保留已提交结果 | 可能 running/failed，由 recovery 解释 | warning | 下一周期重新竞争 |
| database_error/locked | 本次失败，可重试 | 不创建虚假 running；若已创建则按失败路径 | warning，脱敏错误 | 抖动退避 |
| release 失败 | 不覆盖已提交业务 | 不改变成功业务结果 | warning | 等待 TTL/下轮 |

注意：

- busy 不是错误，不能每个 worker 每轮都打 error。
- 不允许在租约失败时插入“skipped/attempt” run，以免污染 run_key 与状态语义；观测用日志和后续指标即可。
- `LeaseBusyError`/`LeaseLostError` 不应导致 Web 应用启动失败；只有服务自身资源初始化失败才影响启动。

## 8. run recovery 策略

### 8.1 当前实现事实

`recover_stale_runs()`（141-157）：

- cutoff = now - 5 分钟，条件 `status='running' AND started_at < cutoff`（严格小于，恰 5 分钟不恢复）；
- 带 guard 时通过 guarded write，把旧 run 置为 `abandoned` 并写 `finished_at` 与中文 last_error；
- 在 `run_tick()` 获取新租约后、创建新 run 前调用（719）。

### 8.2 恢复语义

- 旧 run 标记 abandoned 必须持有当前有效租约和 fencing，避免无锁恢复与有效执行冲突。
- 5 分钟阈值显著大于 30 秒 TTL：进程短暂阻塞、SQLite locked 或 GC 暂停不会立即误判。
- 进程在“部分提醒已提交、run 未结束”崩溃时：提醒保留；旧 run 在后续持锁 tick 中变 abandoned；业务去重由 `dedup_key UNIQUE` 防止重复。
- 新持有者不能把属于自己的新 run 与旧 owner run 混写；`_finish_run` 的条件包含 owner_token 与 fencing（313-315 一带）。

### 8.3 自动/手动/recovery 竞争与 run_key/catch-up（P0-4）

维持接入前阻断。当前 `_new_run_key(now)`（120-122）只生成 `manual:{iso}:{uuid}`，**没有任何 scheduled/recovery 前缀或时间桶生成逻辑**。已认可的方向：自动调度使用与手动运行不同的 `scheduled` key 命名空间；候选方案为 `scheduled:<固定时间桶>`；不新增无限历史回溯；复用现有 `review_candidate()` 的到期窗口判断；超出既有业务窗口的候选不自动补生成。但具体桶粒度、时区、时间边界、同一桶重试语义和 scheduled/recovery 的 key 关系尚未最终裁定，**不得擅自决定具体桶长度（例如 ISO 分钟槽）**。

**必须分别解释的三个「窗口/粒度」概念：**

1. **run_key 桶粒度**决定「同一调度周期如何映射到唯一的 run_key，以及重复唤醒时是否命中同一 run」。它只影响 run 记录的幂等键，**不决定**要生成哪些提醒。
2. **catch-up 窗口**决定「停机后重启，是否/如何补生成漏掉的到期提醒」。当前源码**未定义**任何 service 层 catch-up；不得把「漏跑自动补所有历史周期」当作已批准语义。
3. **`review_candidate()` 现有业务窗口**决定「一条 review 类提醒在什么时间范围内算有效候选」：`if not scheduled <= now < expiry: return None`（`app/reminders.py:532`），daily 为 `[scheduled, scheduled+2h)`、weekly 为 `[scheduled, scheduled+24h)`。这是业务规则的窗口，**独立于** run_key 桶和 catch-up；现有测试（`tests/test_reminder_scheduler_recovery.py:45`、`:66`）证明的仅是这一层业务窗口，不是 service 层停机重启集成。

**run recovery 与停机期间到期提醒的区别（不得混同）：**

- `recover_stale_runs()`（141-157）只把「已存在的 running run 且 started_at 早于 5 分钟」标记为 abandoned；它**不负责**补生成停机期间漏掉的到期提醒。
- 停机期间「应该到期但从未被 tick 扫描」的提醒，是否会补生成，完全取决于上面第 2 点的 catch-up 决策；当前未定义。

**相同时间桶重试与 key 区分（待裁定）：**

> **2026-10-14 状态更新：`scheduled:{epoch 秒 // poll 间隔}` 桶键已在 C-C-2c 实现（`app/reminder_scheduler_service.py` `_submit_tick` 与 `run_scheduled_tick` 两处生成）。已实现事实：**
> - 桶 = `int(now.timestamp() // poll 间隔)`，UTC epoch 对齐，跨进程确定；桶寿命 = poll 间隔（默认 60 秒）。
> - 同桶语义：`run_tick` 对已存在的 run_key（**无论 succeeded/failed/running/abandoned**）直接返回既有记录（`app/reminder_scheduler.py` run_key 查重与 `_create_run` 回退），即同桶首跑定终身；失败桶在下一桶自然重试，最大延迟一个 poll 周期。
> - 无 catch-up：停机漏掉的桶不补 run 记录；由于 tick 为状态驱动扫描（到期提醒在下一次实际 tick 处理），漏桶不丢业务，仅 run 记录不完整。
> - `recovery` 前缀（`TRIGGER_RECOVERY` 已定义）未使用；`tick_once()` 默认 `run_key=None` 也会生成 `scheduled:` 桶键（与自动循环可能同桶）。
> - 已知隐患：桶键计算存在两处（服务用 `self._poll`，`run_scheduled_tick` 回退用常量 `DEFAULT_POLL_SECONDS=60`），poll 配置 ≠ 60 时不一致；`scheduled:{bucket}` 键格式与同桶幂等尚无直接专项测试。
> - **以上为现状描述，不构成对桶语义的批准；P0-4 桶粒度/时区/失败重试/`recovery` 键/补跑仍待用户裁定。**

**运行层面的既有事实（非新语义）：**

- 三者（手动/自动/recovery）都必须先取得同一全局租约；未持锁不得调用 recovery 改变旧 run。
- recovery 不需要单独后台循环；作为持锁 tick 的第一步即可（`run_tick` 719 行调用）。


## 9. 事务边界、fencing 有效性与外部副作用

### 9.1 已确认的事务事实（源码）

- 每个调度器拥有的写操作都是独立的短事务：guarded write 以 BEGIN IMMEDIATE 开始，在同一事务内提交前再次校验 owner/lease/fencing/expires_at/released_at，然后 COMMIT（app/reminder_scheduler.py:203-252）。
- 单条提醒创建依赖 reminders.dedup_key UNIQUE（app/main.py:702）与先 INSERT 后处理 IntegrityError 的去重逻辑（app/reminders.py:664-683）；单条状态推进以 id + 当前状态为条件（app/reminders.py:720-775）。
- 候选级隔离的 savepoint() 帮助函数存在（app/reminder_scheduler.py:436-453），但当前生产路径 _create_candidate_safely()（555-568）通过每条独立 guarded 事务实现隔离，并未在 run_tick 主流程中调用 savepoint；savepoint 目前只被测试直接使用。这是源码事实，不应在文档中描述为“候选都包在 SAVEPOINT 里”。
- run 创建（_create_run）与 run 结束（_finish_run）分别是独立事务；run_key UNIQUE 保证运行记录幂等。
- 因此一个 tick 内存在多个提交点：recovery、run 创建、若干提醒创建/取消/过期、due 推进、run 结束。它们不是一个大事务。

### 9.2 fencing 能与不能保证的边界

可以保证（已有测试支撑，见 tests/test_reminder_scheduler_fencing.py）：

- 旧 owner 在租约过期并被新 owner 接管后，不能再提交任何新的 guarded 事务（创建提醒、transition、expire、恢复旧 run、结束旧 run 都会被 BEGIN 时或 COMMIT 前的身份校验拒绝）。
- 两个独立连接 / 两个真实 OS 进程同时调用 run_tick()，最终只有一个 run，租约最终被持有者释放。

不能据此推定：

- fencing 不会回滚“接管发生之前”旧 owner 已经 COMMIT 的提醒或状态变更。租约在 30 秒 TTL 内若无人续租而被接管，旧 owner 在接管前已提交的业务结果保留；这是 at-least-once 片段提交语义，不是 exactly-once。
- 不受 fencing 保护的写路径（例如普通 HTTP API 直接调用 app/reminders.py 的状态/设置写函数，以及未来可能新增的非调度写入口）不会与调度结果竞争。当前 app/main.py 尚未注册提醒 HTTP 路由，所以这类入口目前主要是测试直接调用；实现批次若新增提醒 API，必须明确哪些写允许在无 guard 下发生。
- heartbeat_lease() 自身只做身份匹配的 UPDATE（app/reminder_scheduler_lock.py:284-325），它不检查“业务连接是否仍健康”；网络/磁盘停顿恢复后，旧持有者仍可能在 TTL 内续租成功。这属于正常租约语义，但要求 heartbeat 间隔明显小于 TTL。

### 9.3 部分成功与外部副作用

- tick 中途 fatal（例如 _advance_due 抛 sqlite3.DatabaseError）时，已创建的提醒保留，run 被标记 failed 且计数器反映已完成部分（tests/test_reminder_scheduler_reliability.py:379-407 已验证）。
- 当前调度器没有外部通知副作用（不发邮件/推送/LLM 调用，app/reminder_scheduler.py:1-5 文件头明确）。因此现在“事务与外部副作用原子性”问题尚不存在；但未来一旦加入通知投递，必须把“提醒已创建”与“通知已投递”设计成可重试/可去重的两步（通知侧独立幂等键），不能假设它们与 SQLite 事务原子提交。
- run_tick() 在获取租约前会 commit 连接上遗留的调用方事务（app/reminder_scheduler.py:698-699）。自动调度使用服务自建的专用连接时没有调用方遗留事务，风险可控；但服务实现不得把该连接与任何 HTTP 请求共享，以免提交不属于 tick 的事务。

## 10. 可观测性方案（最小实用集）

复用现有标准库 logging（项目已使用 personal_ai_os.api、personal_ai_os.scheduler logger），不引入新依赖。建议新增 logger 名称 personal_ai_os.reminder_scheduler。

### 10.1 结构化日志事件与字段

| 事件 | 级别 | 建议字段 |
|---|---|---|
| service_start / service_stop | info | state, poll_seconds, ttl_seconds, enabled |
| startup_failed | error | stage, error_code, error(脱敏) |
| tick_begin | debug/info | run_key, trigger_type, owner_token(截断), fencing(获取后) |
| tick_end | info | run_id, status, duration_ms, created/updated/skipped/due/expired/cancelled/recovered/error 计数 |
| lease_acquired | debug | owner_token, fencing, ttl_seconds |
| lease_busy | debug | holder_owner(截断), holder_expires_at（正常结果，不用 error） |
| heartbeat_ok / heartbeat_failed | debug / warning | fencing, next_expires_at, error_code |
| lease_lost | warning | fencing, stage(丢失时所处阶段), last_committed_count |
| fencing_rejected | warning | write_kind(reminder_create/transition/expire/recover/finish_run), fencing |
| recovery_done | info | recovered_run_count, oldest_running_age_s |
| backlog_seen | warning | due_count, oldest_due_age_s, active_running_runs |
| database_error | warning | error_code, busy?, retry_in_ms（错误信息必须经 sanitize_error 脱敏） |
| shutdown_timeout | error | waiting_ms, tick_stage, hint(依赖 TTL + recovery) |

约束：owner_token 含 hostname/uuid，日志只记录前 16-24 字符或其哈希；last_error 继续走 sanitize_error()（app/reminder_scheduler.py:71-78），禁止输出绝对路径、原始 SQL、密钥样式串。

### 10.2 指标（先以 run 表/日志派生，不新增表）

- 现阶段无需新增 metrics 表或依赖。以下指标可从 reminder_scheduler_runs 与日志派生：
  - 最近一次 succeeded/partial_failed/failed 时间与耗时；
  - 连续 lease_busy 次数、连续 database_error 次数；
  - 每分钟 created/due/expired 计数；
  - running 状态 run 的数量与最大年龄（对应崩溃未恢复）；
  - 最近一次 recovery 恢复的 run 数量。
- 简单告警阈值（人工或后续监控）：出现 abandoned run；同一 run 长期 running 超过 5 分钟仍未被恢复；连续 >=3 次 database_error；出现 shutdown_timeout；lease 在多个 fencing token 间高频翻转（抖动/多实例时钟异常信号）。
- run 历史保留策略仍未实现（第 13 章已列为未决），不得在自动调度批次顺手清理历史 run。

## 11. 自动调度测试矩阵（接入前验收）

约定：全部使用 tests/conftest.py 的临时库保护（tmp_path + DATABASE_PATH + monkeypatch app.main.DB_PATH），多进程用真实 subprocess（沿用 test_reminder_scheduler_lock.py / fencing 的写法），不触碰 data/personal_ai_os.db，不访问网络；时钟用显式注入，不 sleep 真实 TTL。

状态说明：下表「阻断项」列对应接入前验收；除已标「已有测试」的行外，其余均为**建议新增测试**，尚未实现或执行。不得把建议新增的测试描述为已通过。

| # | 场景 | 前置条件 | 执行方式 | 预期结果 | 验证手段 | 阻断项 |
|---|---|---|---|---|---|---|
| 1 | 正常启动与停止 | 临时库 init_db | 实例化 service，start 后 stop | 恰好一个 loop；无 tick 重叠；executor 被关闭 | is_running() 状态、日志事件、后台线程/任务计数 | 是 |
| 2 | 生命周期重复调用 | service 已 running | 连续 start() 两次、stop() 两次 | 不创建第二个 loop；重复 stop 不抛异常 | 状态断言、提交计数桩只执行一轮 | 是 |
| 3 | 启动失败清理 | 令连接工厂/executor 初始化抛错 | start() 捕获并再次 start/stop | 状态回 stopped/可重启；无 loop/线程泄漏；startup 日志完整 | 状态机断言、线程枚举 | 是 |
| 4 | 两个 scheduler 竞争同一租约 | 同库两个 service 实例 | 屏障同时触发一次 tick | 一个执行一个 busy；只有一个 run；busy 不写 run | run 计数、owner/fencing 与 lease 一致（参照现有 fencing 测试） | 是 |
| 5 | tick 超过租约 TTL | ttl 设短（如 1s），候选处理注入延迟 | 单次 tick 跨越 TTL | 续租生效则继续；未续租则后续 guarded 写被拒且 tick lease_lost 中止 | run/reminder 最终状态、fencing_rejected 日志 | 是 |
| 6 | heartbeat/续租失败 | 在阶段边界令 heartbeat_lease 返回 database_error | 运行一个 tick | tick 记为可重试失败；不伪装成功；退避生效 | run 状态、退避时间桩、日志 | 是 |
| 7 | 租约丢失后 fencing 拒绝（P0-1） | 旧 owner 获取租约后暂停，等待 lease 过期，新 owner 接管，旧 owner 恢复 | 旧 owner 恢复后继续走完整 run_tick 主流程尝试写入 | 旧 owner 所有后续 guarded 写抛 LeaseLostError；新 owner 正常；最终数据库状态可断言 | 独立连接或真实 OS 子进程的最终 owner/fencing 断言（现有 test_stale_guard_* 仅同连接/单函数，不可视为已满足本项） | 是 |
| 8 | tick 执行中 shutdown | tick 在候选处理中途被阻塞 | start 后立即 stop，释放阻塞 | 不再提交新 tick；当前 tick 在阶段边界收敛；连接关闭 | loop 退出、无新 run、连接关闭断言 | 是 |
| 9 | shutdown 超时 | tick 阻塞超过宽限时间 | stop() 使用短 grace | stop 在有界时间返回；记录 shutdown_timeout；不杀线程；依赖 TTL+recovery | stop 耗时上限、日志、进程仍可退出 | 是 |
| 10 | 进程异常退出后的恢复 | 制造 running run 且 started_at 早于 5 分钟 | 杀子进程后由新进程跑 tick | 旧 run 被标记 abandoned，计数 recovered_run_count；不覆盖新 run | run 状态与 finished_at、last_error | 是 |
| 11 | 重复提醒与幂等 | 启用同一规则，重复 tick / 同 dedup_key 并发 | 连续 tick + 双连接同 key 插入 | 同一 dedup_key 仅一条；重复 run_key 返回已有 run | reminders/runs 计数（复用 reliability 去重测试） | 是 |
| 12 | SQLite 锁竞争 | 一个连接持写锁；busy_timeout=1000ms | scheduler tick 与外部写并发 | 触发 database_error/locked 路径并退避，不崩溃、不伪造成功 | 退避桩、错误分类日志 | 是 |
| 13 | 多 worker 重复调度风险 | 两个真实 OS 进程各自启动 service 循环 | subprocess 同时运行有限轮循环 | 每个调度周期至多一个进程实际执行；无重复提醒；busy 日志高频但无 error 风暴 | run 数 <= 周期数、reminder 去重、日志级别 | 是 |
| 14 | 测试隔离/真实库保护 | 不设置临时 DATABASE_PATH | 直接收集/运行新测试 | conftest 守卫拒绝；任何生命周期测试断言 DB 在 tempdir | conftest autouse 断言、路径断言 | 是 |
| 15 | Uvicorn --reload 父子进程 | 临时库启动 uvicorn --reload 子进程 | 触发一次 reload 后关闭 | 新旧进程不同时长期持有租约；旧进程租约被释放或过期接管；无残留 run | lease 表 owner 变化、进程退出码 | 否（强烈建议，不独立阻断） |
| 16 | 自动与手动 tick 竞争 | service 循环运行中并发手动 run_tick | 同时触发 | 由同一 lease 串行化；二者 run_key 前缀不同且都可幂等重放 | run 记录、无重复业务写入 | 否（API 批次前必须） |
| 17 | 长 tick 中阶段边界停止标志 | tick 内注入停止事件检查点 | shutdown 与长候选列表 | 到达边界即放弃剩余候选，已提交部分保留并可解释 | created 计数小于总量、run failed/partial 语义 | 否 |
| 18 | release 失败不掩盖结果 | 令 release_lease 返回 database_error | 正常 tick | tick 业务结果仍返回成功；run 标记 release 异常元数据；租约靠 TTL 回收 | 返回值、日志、后续接管 | 否 |

说明：#1-#14 为接入自动调度前的阻断性验收项；#15-#18 可在首个启用版本前补齐或在紧随其后的加固批次完成，但 #16 在提供任何手动 tick HTTP 入口前必须完成。

关于 #15（uvicorn --reload）：维持「强烈建议，不独立阻断」。理由：当前默认启动命令（`AGENTS.md` / `run.bat`）不含 `--reload`；其多进程正确性由 #4/#13 等接入前阻断项兜底。这**不代表** reload 场景已经通过测试——若决定在开发中启用 `--reload` 自动调度，必须在启用前完成 reload 专项验证，不得把推断描述为已有测试证明。

关于 #19（停机 catch-up）：不新增独立阻断编号，并入 P0-4 的 catch-up 决策。现有业务窗口测试（`tests/test_reminder_scheduler_recovery.py:45/66`）与未来 service 层停机重启集成测试是两回事；后者必须在 P0-4 裁定后、在 C-C-2c-2/4 中补充，验证「停机→重启→仅生成窗口内提醒、不回溯过期候选」。

## 12. 分阶段实施顺序与验收标准（供后续批次，不在本批实施）

1. C-C-2c-1 纯服务骨架：ReminderSchedulerService 的 start/stop/is_running、状态机、单线程 executor 与独立连接工厂，但不挂 lifespan、默认 enabled=false。验收：测试矩阵 #1-#3、#8、#14 通过；不调用真实库。
2. C-C-2c-2 续租与长任务安全：在 run_tick 阶段边界接入 heartbeat（复用 heartbeat_lease，禁止共享连接心跳线程）、软上限与停止标志。验收：#5、#6、#7、#9、#17；确认 _heartbeat_if_needed 被真实调用或被明确重构。
3. C-C-2c-3 生命周期接线：在 app/main.py 生命周期中按顺序启停（推荐与 on_event -> lifespan 迁移同批或紧随），新增显式启用配置（默认关闭）。验收：#4、#8、#13、#15；TestClient 下无后台任务泄漏；弃用警告若迁移 lifespan 应消失。
4. C-C-2c-4 竞争与恢复加固：SQLite locked 退避、抖动、recovery 竞争、release 失败路径、可观测性事件落地。验收：#10-#12、#18，日志字段齐全且脱敏。
5. C-C-2c-5（可选）手动触发入口与 run 保留策略：手动 tick API 的 lease_busy/lost/database_error 映射（409/409/503）、run 清理。验收：#16；清理不影响 abandoned 可追溯窗口。

每阶段验收都必须运行 compileall 与相关 pytest，并在 docs/testing.md 记录本轮真实命令与结果；历史结果不得冒充本轮结果。

## 13. 待人工批准决策与禁止的假设

### 13.1 待人工批准决策（必须在进入 C-C-2c-1 前由人工书面裁定）

P0 级（四项，均维持接入前阻断）：

- **P0-1（测试 #7）**：确认「跨连接/OS 子进程接管 + 完整 `run_tick()` 流程」的端到端 fencing 用例必须作为 #7 的验收内容新增；确认现有同连接 `test_stale_guard_*` 不满足 #7。此方向已由用户确认，实现前无需再议，但验收用例尚未编写。
- **P0-2（heartbeat）**：确认 `_heartbeat_if_needed()` 必须接入有效调用路径或明确删除/重构；**待裁定具体触达点**（每 N 个候选 vs 固定秒数）与 tick 软上限倍数。当前不得把具体数值写成已批准。
- **P0-3（shutdown）**：确认「有限等待、不无限阻塞、超时记录 `shutdown_timeout`、不声称已取消」方向；**待裁定最终方案**（见 6.2 三选一）+ 宽限时间具体数值（35s 仅为候选，未实测校准）。
- **P0-4（run_key/catch-up）**：确认「`scheduled` 命名空间、不新增无限回溯、复用 `review_candidate()` 窗口、过期候选不补」方向；**待裁定具体桶粒度、时区/时间边界、同桶重试语义、`recovery` 键关系**。禁止自行采用 ISO 分钟槽等具体值。

P1 级（非接入前阻断，但需在相应批次前裁定）：

- 自动 tick 默认开启还是默认关闭（本设计建议默认关闭或显式环境变量启用，首次启用仅限本地 loopback）。
- 基础轮询间隔（建议 60s，下限 30s）与 lease busy 抖动参数。
- on_event 迁移到 lifespan 的批次归属。
- run 历史保留周期与清理方式。
- 是否/何时引入通知投递及其幂等键。
- 测试矩阵 #16（自动与手动 tick 竞争）在提供任何手动 tick HTTP 入口前必须完成。

### 13.2 明确禁止的假设

- 不得假设“有租约 + fencing”就解决了所有多进程问题（已提交片段、锁竞争、时钟与长阻塞仍需测试）。
- 不得假设 cancel asyncio task 能中断线程池中的同步 sqlite3 调用。
- 不得假设多个进程共享一个 sqlite3 连接是安全的。
- 不得假设 `ThreadPoolExecutor.shutdown(wait=False)` 能保证进程在后台线程仍运行时退出；不得把 daemon 线程、线程池内部线程与 Uvicorn 关闭行为混为一谈。
- 不得把自动调度描述为已启用；当前 app/main.py 没有任何 reminder 自动循环。
- 不得假设多机/网络盘共享 SQLite、公网多用户或 exactly-once。
- 不得在没有真实库守卫的情况下运行生命周期测试。
- 不得假设 `_heartbeat_if_needed()` 已生效（当前是孤立函数，未接入 `run_tick()`）。
- 不得假设自动 run_key 会按某个具体时间桶或回溯窗口工作（当前 `_new_run_key` 只有 manual 前缀）。
- 不得把建议、推断或未来计划改写为已实现事实。

## 14. 最终接入自动调度前的阻断条件（必须全部满足）

1. 测试矩阵第 11 章 #1-#14 全部通过，且含至少两个真实 OS 子进程用例（竞争、异常退出恢复）。
2. **P0-1**：测试矩阵 #7 的「跨连接/OS 子进程接管 + 完整 `run_tick()` 流程」端到端 fencing 用例已实现并执行；不得以现有同连接 `test_stale_guard_*` 代替。
3. **P0-2**：`run_tick` 的长 tick 续租路径被测试实际覆盖；不存在“函数定义了 heartbeat 但主流程不调用”的落空路径（`_heartbeat_if_needed` 要么真实接入，要么已明确删除/重构并记录替代方案）。
4. **P0-3**：shutdown 有有界等待与超时日志，且经测试证明不会无限挂起 Uvicorn 关闭；「不杀线程」与「进程最终能退出」两个断言分别验证，不得依赖未证明的 recovery 来掩盖进程退出风险。
5. **P0-4**：自动 `run_key` 的桶粒度、catch-up 窗口、同桶重试与 `scheduled`/`recovery` 键关系已经人工裁定并落地到测试；禁止假设漏跑自动补所有历史周期。
6. 自动调度有显式启用开关，默认行为经人工确认；TestClient/测试环境默认不遗留后台循环。
7. 所有调度器写路径仍在 guarded write 内；任何新增非 guard 写入口都已在文档与测试中单独说明。
8. 测试全程使用临时库，tests/conftest.py 真实库保护保持开启；真实开发库哈希在实现批次前后可核对。
9. 本设计文档经人工审核批准；P0-1 至 P0-4 未解决前不得启用自动调度，未批准前不进入 C-C-2c-1。

## 15. 本设计批的操作边界声明

- 本批仅新增/完善本设计文档与交接文档；未修改 app/ 下任何生产代码、测试、schema、配置或前端。
- 本批未调用 run_tick()，未启动后台循环，未对真实开发数据库执行初始化/迁移/租约操作/写入。
- 本批未运行 pytest（C-C-2c-0 为只读设计审查；文中第 2.2 节测试结果均为引用 C-C-2b 历史记录，非本轮执行）。
- 当前目录不是 Git 仓库（git rev-parse 返回 fatal: not a git repository），因此无法提供 git diff，亦未执行 git init。


---

## 16. C-C-2c 实施结果附录（2026-10-13 19:10 CST）

本附录记录后续实施事实；第 1-15 章保留设计阶段的历史推导和源码事实。

### 16.1 已实施

- FastAPI lifespan 替代 `@app.on_event`；先校验 scheduler 配置，再执行 `init_db()`，随后启动后台 scheduler。
- 新增 `app/reminder_scheduler_service.py`：
  - `ReminderSchedulerService`；
  - asyncio 控制循环；
  - daemon 单线程 worker；
  - 独立 SQLite 连接；
  - start/stop 幂等；
  - busy/lost/database_error/stopped 状态分类；
  - health 快照；
  - tick started/completed/failed 与 elapsed_ms 等脱敏日志；
  - service 构造/启动失败时回滚清理 ConnectorScheduler。
- `run_tick()` 增加可选 `clock`、`should_stop`、`heartbeat_interval_seconds`、`max_tick_seconds` 参数：
  - heartbeat 默认 10 秒；
  - tick 软上限 90 秒；
  - recovery、run 创建、粗阶段、task/recommendation 候选批次和 due 批次边界检查。
- scheduled run key：`scheduled:<按 poll interval 归一化的时间桶>`。
- shutdown grace 默认 35 秒；超时不杀线程，服务报告 `stopped_with_active_tick`。
- 默认配置：`REMINDER_SCHEDULER_ENABLED=0`、poll 60 秒、grace 35 秒。

### 16.2 验证结果

- 生命周期测试：33 passed。
- 提醒相关全专项：123 passed。
- 全量测试：445 passed、1 skipped、1 warning。
- collect-only：446 collected。
- 真实开发库 SHA-256、大小和 mtime 前后不变。
- 唯一 skip 是当前环境不能创建 symlink；唯一 warning 是 Starlette TestClient/httpx 兼容性提示。

### 16.3 仍不承诺

- 不承诺 exactly-once；自动 tick 是多个短事务，存在 at-least-once 片段提交语义。
- 不补齐所有历史漏跑周期；只保证同时间桶 run key 幂等。
- 不包含通知发送器、通知幂等记录、手动 tick HTTP API 或 run 清理。
- 不强杀 shutdown 超时后的同步 SQLite worker；依赖 lease TTL、fencing 和 recovery。
- 未做真实 `uvicorn --reload` E2E；已有真实 OS 子进程竞争/恢复测试。
