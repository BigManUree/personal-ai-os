# C-C-2d：通知投递基础设施与幂等记录设计/实施记录

更新时间：2026-10-13 20:26 CST
状态：已实施并通过通知/提醒回归；真实外部渠道未实施，通知消费器默认关闭。

## 1. 范围与安全边界

本阶段只建立本地通知 outbox、投递状态机和 `fake_test` 测试渠道：

- 不发送邮件、短信、Webhook、桌面通知或云通知；
- 不提供任意 URL、任意 Python import 或真实 channel loader；
- 即使 `NOTIFICATION_DELIVERY_ENABLED=1`，服务也只接受 `name == "fake_test"` 的渠道；
- 生产事件由提醒调度在同一 SQLite guarded transaction 中持久化；
- 所有数据库测试使用系统 Temp 下的独立 SQLite 文件。

事件/投递记录已持久化只表示“有待发送意图”，不表示外部消息已经送达。`succeeded` 仅表示 fake channel 返回成功。

## 2. 领域模型

### 2.1 `reminder_notification_events`

业务事实：某个提醒在某个稳定业务时机进入 `due`。

- `event_key`：数据库级唯一键。
- `event_type`：当前仅 `reminder_due`。
- `reminder_id`：外键到 `reminders(id)`，级联删除。
- `source_type/source_id`：从提醒冗余，便于审计。
- `payload_json`：通知展示所需字段，长度上限 5000，不包含密钥。

### 2.2 `reminder_notification_deliveries`

某事件在某渠道/接收者上的投递生命周期。

- `delivery_key`：数据库级唯一键。
- 当前渠道固定 `fake_test`，接收者固定 `local`。
- 状态：`pending`、`running`、`retry_waiting`、`succeeded`、`dead_letter`、`cancelled`。
- 字段包括尝试次数、最大次数、可领取时间、下次重试时间、claim 锁、成功/死信时间、脱敏错误码、错误摘要、外部消息 ID。

分两张表的理由：事件是业务事实，投递是渠道生命周期；未来多渠道可复用同一事件。本阶段不为未来渠道预先建立空抽象或真实发送器。

## 3. 幂等键

首次 scheduled -> due：

```text
reminder.due.v1:<reminder_id>:<scheduled_at>
```

每次 snoozed -> due 使用新的业务时机：

```text
reminder.due.v1:<reminder_id>:<snoozed_until>
```

Delivery key：

```text
<event_key>:fake_test:local
```

重复 tick 使用 `ON CONFLICT DO NOTHING RETURNING` 幂等创建；即使并发连接同时写入，数据库唯一约束也是最终防线。

## 4. 事务边界

`app/reminder_scheduler.py::_mark_due_one()` 在同一个 `guarded_write()` 中：

1. 校验 lease owner/token/fencing；
2. `reminders` 从 scheduled/snoozed 变为 due；
3. 插入 notification event；
4. 插入 fake_test pending delivery；
5. 提交前再次校验 lease/fencing。

通知插入失败会回滚提醒 due；后续 fake channel 投递失败不会回滚已经完成的提醒状态。闭源外部渠道接入时也应遵守 outbox 边界。

## 5. Claim、重试、死信与恢复

Claim 使用单独 SQLite 原子条件更新：

```sql
UPDATE ... SET status='running'
WHERE id=(SELECT id ... WHERE status IN ('pending','retry_waiting') ...)
RETURNING *
```

策略：

- 最大尝试次数：5；
- 初始退避：30 秒；
- 指数退避：30、60、120、240、480 秒；
- 延迟计算上限 3600 秒；
- 不使用随机 jitter，保证测试确定；
- 临时错误/超时：未达上限进入 retry_waiting，达上限进入 dead_letter；
- 永久错误/协议错误/渠道白名单错误：立即 dead_letter 或拒绝启动；
- stale `running`：超过 300 秒保守重置为 retry_waiting；若已达最大次数则 dead_letter。

“发送成功但本地确认前崩溃”在未来真实渠道中可能造成重复发送；当前只保证 at-least-once，不承诺 exactly-once。

## 6. Fake channel 与消费服务

`FakeNotificationChannel` 是内存测试替身：

- `DeliverySuccess(external_message_id=...)`；
- `DeliveryTransientError`、`DeliveryTimeoutError`、`DeliveryPermanentError`、`DeliveryConfigError`；
- 可脚本化连续结果并记录调用；
- 不访问网络、文件、URL 或凭据。

`NotificationDeliveryService`：

- asyncio loop 只负责定时和状态；
- 同步 SQLite + channel 调用在 max-workers=1 的 daemon worker 上串行执行；
- 默认 `NOTIFICATION_DELIVERY_ENABLED=0`；
- poll 环境值允许 5..3600 秒（测试可注入更小）；
- shutdown timeout 允许 1..300 秒；
- shutdown 等待有界，超时状态为 `stopped_with_active_delivery`，不假报线程完全停止；
- `/api/health` 增加嵌套 `notification_delivery` 对象，保持顶层字段兼容。

## 7. FastAPI lifespan

启动顺序：

1. 校验 reminder 配置；
2. 校验 notification 配置；
3. `init_db()` 幂等创建通知表；
4. 启动 ConnectorScheduler；
5. 启动 ReminderSchedulerService（producer）；
6. 构造/启动 NotificationDeliveryService（consumer，默认 no loop）。

关闭顺序：reminder producer -> notification consumer -> connector。

## 8. 已测性质

`tests/test_notification_delivery.py` 覆盖：

- 稳定键、跨 snooze 区分、DB 唯一约束、跨连接幂等；
- pending claim、多连接/真实 OS 双进程 claim 至多一个成功；
- success、transient、timeout、permanent、5 次上限、dead letter；
- stale running 恢复、错误脱敏；
- fake channel 成功/失败/超时脚本；
- 提醒 due 与 event/delivery 同事务提交、重复 tick 不重复通知、quiet hours 不生成、过期/取消不新增 due 事件；
- 默认 disabled、显式 enabled 仍只用 fake_test、start/stop 幂等、进程重启恢复、有界 shutdown；
- lifespan 配置校验、实例注册/清理和 health。

## 9. 明确未做

- 无真实邮件、短信、Webhook、桌面通知、云通知渠道；
- 无通知 HTTP API、前端通知中心、手动重放/死信管理界面；
- 无独立迁移框架；
- 不承诺 exactly-once；
- 未做真实 `uvicorn --reload` 父子进程 E2E；
- 不自动删除死信。
