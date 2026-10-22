# 架构说明

更新时间：2026-10-13 20:25 CST

## 1. 当前形态

Personal AI OS MVP 是本地优先、单用户的 FastAPI + SQLite 应用。前端是 `frontend/` 下的 Vue 3 + Vite 工程，构建产物（`index.html` + `assets/index-<hash>.js|css`）输出到 `app/static/`，由 FastAPI 静态文件挂载提供。

- HTTP 入口：`app.main:app`
- 数据库：默认 `data/personal_ai_os.db`，可用 `DATABASE_PATH` 覆盖
- Web 服务：Uvicorn，默认 loopback `127.0.0.1:8000`
- 云端模型：OpenAI 兼容 Chat Completions API；无 Key 时聊天使用本地演示逻辑
- 自动提醒：生命周期已接入，但通过 `REMINDER_SCHEDULER_ENABLED=0` 默认关闭
- 通知 outbox/fake 消费：提醒 due 事务内持久化 event/delivery；消费器通过 `NOTIFICATION_DELIVERY_ENABLED=0` 默认关闭
- 真实通知投递：未实现邮件、短信、Webhook、桌面通知或 LLM 通知；当前只允许 `fake_test` 测试渠道

## 2. 运行时分层

项目继续采用“Fat entrypoint + 领域模块”的扁平结构：

1. `app/main.py` 负责 FastAPI 实例、lifespan、中间件、HTTP 路由、schema 创建/增量迁移和静态资源。
2. 领域模块承担可测试业务逻辑：
   - 长期记忆：`memory_extract.py`、`memory_match.py`、`retriever.py`
   - 文档：`document_parse.py`、`document_chunk.py`
   - 画像和建议：`profile_propose.py`、`recommendation_engine.py`、`recommendations.py`、`recommendation_feedback.py`、`recommendation_stats.py`
   - 行为分析：`behavior_events.py`、`analytics.py`
   - 连接器：`connectors.py`、`github_connector.py`、`httpapi_connector.py`、`connector_scheduler.py`、`secret_crypto.py`
   - 提醒：`reminders.py`、`reminder_scheduler.py`、`reminder_scheduler_service.py`、`reminder_scheduler_lock.py`
   - 通知：`notification_delivery.py`、`notification_delivery_service.py`（当前仅 fake-test 渠道）
3. `frontend/src/`（Vue 3）只调用 HTTP API，不直接访问数据库或密钥；`app/static/` 是其构建产物，不手改。
4. `tests/` 使用临时数据库、显式参考时间、httpx mock、真实多连接和真实 OS 子进程验证并发语义。

本批没有机械拆分 `main.py`。lifespan 接入只做生命周期所需最小改动，避免同时搬迁 80+ 路由。

## 3. 启动与关闭流程

FastAPI 使用 `lifespan`：

1. Uvicorn 导入 `app.main:app`，模块级代码解析 `DATABASE_PATH` 并注册路由/中间件。
2. lifespan 首先读取并校验 reminder scheduler 与 notification delivery 配置；非法 `REMINDER_SCHEDULER_*` 或 `NOTIFICATION_DELIVERY_*` 值会在 `init_db()` 和任何 scheduler 启动前快速失败。
3. 配置有效后调用 `init_db()`，执行幂等建表和增量迁移。
4. 创建/复用并启动全局 `ConnectorScheduler`。该调度器仍只处理逐源 opt-in 的数据源同步。
5. 创建全局 `ReminderSchedulerService`；默认 disabled 时不创建 asyncio loop 或执行器。显式启用时启动一个 asyncio 控制循环和一个 daemon 单线程 worker。
6. reminder 启动后创建 `NotificationDeliveryService`；即使显式启用也只注册 `fake_test` 渠道。默认 disabled 时不创建 asyncio loop/执行器，待投递记录可安全保留。
7. 若 service 构造或启动失败，lifespan 的 finally 仍按启动逆序停止组件并清空模块级引用。
8. 正常关闭时先停 reminder producer，再停 notification consumer，最后停 connector scheduler。

`/api/health` 保持原有 `status` 和 `mode` 字段，并提供 `reminder_scheduler`、`notification_delivery` 两个嵌套状态对象；旧消费者读取前两个字段不受影响。

## 4. 提醒自动调度机制

调用链：

```text
FastAPI lifespan
  -> ReminderSchedulerService.start()
  -> asyncio loop（定时/状态/观测）
  -> daemon ThreadPool-like 单 worker（同一服务内 tick 串行）
  -> run_scheduled_tick() 打开独立 sqlite3.Connection
  -> reminder_scheduler.run_tick(..., trigger_type="scheduled")
  -> SQLite lease/fencing 与 reminders 领域写入
```

职责边界：

- asyncio loop 只负责定时、状态、分类 busy/lost/database_error 和关闭，不直接执行 SQLite 长任务。
- 每个同步 tick 使用独立 sqlite 连接，连接在 tick 结束后关闭，不与 HTTP 请求共享。
- 同一 `ReminderSchedulerService` 内由单线程 worker 防重叠；多进程/多 worker/热重载以 SQLite lease 为权威。
- `reminder_scheduler_runs.run_key` 使用 `scheduled:<按配置间隔归一化的时间桶>`；同桶重放返回已有 run，不补跑所有历史漏点。
- busy 是正常状态，不写 run、不打 error 风暴；database_error/lease_lost 可从服务快照观测。
- 结构化日志记录 service 启停、tick started/completed/failed、耗时（`elapsed_ms`）以及 lease acquire；heartbeat/release 成功使用 debug，lease lost/release failure 使用 warning/error，日志不包含密钥或业务内容。

## 5. 通知 outbox 与 fake-test 投递

提醒调度在 `_mark_due_one()` 的同一个 guarded SQLite transaction 中执行：

```text
reminder(scheduled/snoozed -> due)
  -> reminder_notification_events(event_key 唯一)
  -> reminder_notification_deliveries(delivery_key 唯一, fake_test/pending)
```

- 事件表示业务事实“某提醒在某业务时机到期”；投递表示该事件在某渠道上的生命周期，二者不能混为“已送达”。
- 首次提醒使用 `scheduled_at`，每个 snooze 周期使用 `snoozed_until` 生成稳定 event key；重复 tick 不重复插入。
- 投递领取由 SQLite 条件 `UPDATE ... RETURNING` 原子完成；asyncio Lock 只防止同进程重入，不负责多进程互斥。
- 状态：`pending -> running -> succeeded`；临时失败进入 `retry_waiting`，永久失败或达到 5 次上限进入 `dead_letter`；旧 `running` 超过 5 分钟保守恢复。
- 退避：30 秒起步、指数增长、无随机 jitter、最大 3600 秒；错误摘要会脱敏 API key/token/password/Authorization。
- 如果 fake channel 已执行但本地确认前崩溃，恢复可能再次调用渠道，因此整体是 at-least-once，不承诺 exactly-once。
- 本阶段没有任意渠道加载器、Webhook URL、邮件/短信/桌面/云通知实现。

## 6. Heartbeat、长任务和协作停止

`run_tick()` 在 acquire lease 后创建 `_TickControl`，在以下边界检查停止、软上限和续租：

- recovery 后、run 创建后；
- 初次过期后、候选生成后、二次过期后、due 推进后；
- task/recommendation 候选批次后；
- due 状态推进批次中。

续租复用 `heartbeat_lease()`，没有新增共享同一 SQLite 连接的 heartbeat 线程。自动 tick 传入 wall clock；手动确定性调用默认使用业务 `now`，保持显式时间语义。

默认 heartbeat 间隔为 10 秒，tick 软上限为 90 秒。到达关闭/软上限边界时，当前 tick 不再启动新的候选或新的 guarded 事务；已提交片段保留，run 收敛为 failed，错误信息脱敏。

## 7. Shutdown 与 fencing

关闭流程：

1. 设置 asyncio stopping event 和线程 stop event。
2. 取消 asyncio 控制循环；`asyncio.shield(wrap_future(...))` 保证取消等待不会取消正在运行的同步 SQLite tick。
3. 有界等待当前 tick，默认 35 秒，可由 `REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` 配置。
4. 若按时结束，关闭执行器并释放 lease。
5. 若超时，stop 仍返回；服务状态为 `stopped_with_active_tick`，`active_tick=true`、`shutdown_timed_out=true`。Python 不强杀线程。
6. 超时后旧线程不能接受新工作；lease 30 秒 TTL 过期后，新 owner fencing token 递增，旧 owner 的后续 guarded write 被拒绝，下一个持锁 tick 把超过 5 分钟的 running run 标记为 abandoned。

该语义不是 exactly-once。一个 tick 内含多个短事务，接管前已提交的提醒/状态片段会保留；去重依赖 `reminders.dedup_key` 和 run 记录幂等。

## 8. 请求和数据流

典型写请求：

`浏览器静态页面 -> API 路由 -> Pydantic 校验 -> 一个 db() 上下文中的 SQLite 事务 -> JSON 响应`

连接器同步仍使用独立 `ConnectorScheduler`，与 reminder lease 不共享锁。连接器密钥由 `secret_crypto.py` 加密，外部 HTTP/GitHub 测试全部 mock。

## 9. 已知架构限制

- P1：`app/main.py` 仍很大，后续应在 API 合约保护下按领域小步提取。
- P1：通知发送器尚未实现；自动 tick 目前只改变 SQLite 中的提醒状态。
- P2：没有独立迁移框架和迁移版本表，仍使用幂等 `init_db()`/辅助迁移。
- P2：当前无登录认证，只能 loopback 使用，不应公网暴露。
- P2：SQLite 方案不承诺多机/网络盘共享、公网多用户或 exactly-once。
