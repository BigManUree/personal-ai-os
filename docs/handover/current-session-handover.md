# 项目上下文交接文档（current-session-handover）

生成时间：2026-10-14 CST
来源窗口：Claude Code 会话（含一次 compact，本文件覆盖 compact 前后全部有效上下文）
用途：新 Claude Code 窗口续接开发。**先读本文件，再核对工作区实际文件，然后等待用户明确指令。**

---

## 1. 项目基本信息

- 项目：Personal AI OS MVP — 本地优先的单用户个人助手应用（提醒、通知、数据源同步、推荐等能力逐步建设中）。
- 路径：`D:\develop\VibeCodingProjects\personal-ai-os-mvp`
- 技术栈：FastAPI + SQLite（WAL 除外，当前无 sidecar 文件）+ 原生 HTML/CSS/JS 前端（`app/static/`，无构建）。Python 3.11+（本机 `.venv` 实测 3.13.9）。
- 启动：`.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000` 或 `run.bat`。应用入口 `app.main:app`。
- 真实数据库：`data/personal_ai_os.db`（当前 352,256 bytes，mtime 2026-10-13 19:49:11.445，SHA-256 `49F1FD751F2E6C522A5D319EFBA94FB22EA21A7D6900B2037FB848EBA164F0D8`）。
- **不是 Git 仓库**（无版本历史可回溯，这也导致部分历史修改无法 diff 追溯）。不要自动 `git init`。
- 当前阶段：C-C-2c 与 C-C-2d 已实现并测试通过（默认关闭）；C-C-2e-0 审计与 m00679 审查已完成。**2026-10-14 用户完成第一轮书面裁定**：R1 保留当前库；P0-1 正式接受；C-C-2d 有条件验收；P0-2 参数追认；P0-3 方案 C + 35 秒有条件追认（遗留验证未完成）；P0-4 候选方案已研究（未批准，**注意：`scheduled:{epoch//poll}` 桶键已在 C-C-2c 实现，此前「P0-4 完全未实现」的描述有误**）。**当前无进行中的开发批次，等待 P0-4 方案裁定。**

## 2. 用户长期要求与不可违反约束

（全部已在 `AGENTS.md` 固化，此处列关键项；AGENTS.md 为权威版本）

安全红线：
- 不删除、覆盖、重建或迁移 `data/personal_ai_os.db` 及任何备份文件；不在真实库上跑测试/seed/修复脚本。
- 不对真实库调用 `run_tick()`、lease acquire/heartbeat/release、通知 claim/delivery。
- `REMINDER_SCHEDULER_ENABLED` 与 `NOTIFICATION_DELIVERY_ENABLED` 默认必须为 `0`；通知即使启用也只允许 `fake_test` 渠道，严禁真实邮件/短信/Webhook/桌面/云通知。
- 不读取输出 `.env`、`data/secret.key` 或任何密钥值。
- 不执行 `git reset --hard`、`git clean -fd`、强制推送；非 Git 仓库不初始化。
- 默认只绑 loopback。

工作方式约束：
- 分批实施：每批完成后停止，输出报告，等待用户验收/授权才进入下一批。
- 测试必须用临时库（`tests/conftest.py` 守卫）；外部网络必须 mock。
- 提交结论前跑 `compileall` + 全量 pytest；历史测试结果必须标注时间，不跨代码状态合并。
- 修改前先读代码/测试/文档，不凭文件名或计划文档判断完成状态。
- 遇到无法证明安全性的数据库/密钥/调度/通知操作：停止该操作，继续安全任务，在交接文档单列人工确认项。

用户已否决/未采纳的方案（背景）：
- 用旧备份直接覆盖当前真实库（有明确丢数风险，审计结论为不建议）。
- asyncio/threading Lock 作为跨进程互斥权威（必须以 SQLite lease/fencing 为准）。
- 任意 Python import / 任意 URL 的通知渠道 loader（安全红线）。

## 3. 项目完整进度总表

| 模块/任务 | 当前状态 | 已完成内容 | 未完成内容 | 证据 | 下一步 |
|---|---|---|---|---|---|
| C-C-2a 提醒存储/规则/状态机/手动 tick | 已完成并验证 | 存储、规则、安静时段、状态机、确定性手动 tick | 手动 tick HTTP API 未做 | docs/progress.md；历史测试绿 | 等待后续批次 |
| C-C-2b SQLite lease/fencing | 已完成并验证 | lease、TTL、heartbeat、fencing、stale recovery | — | docs/progress.md | — |
| C-C-2c 调度生命周期（默认关闭） | 已完成并验证 | ReminderSchedulerService、lifespan、单线程 worker、busy/lost/database_error 状态、有界 shutdown、跨进程测试 | 真实通知渠道（明确不在范围） | tests/test_reminder_scheduler_fencing.py 等 7 项（2026-10-13 实跑通过） | P0-2/P0-3/P0-4 裁定 |
| C-C-2d 通知投递基础设施 | 已完成并验证 | outbox 同事务、幂等键、claim 互斥、退避/死信/stale 恢复、fake_test 渠道、默认关闭 service | 真实渠道、HTTP API、死信重放、保留策略、jitter 决策 | 33 项专项测试 + 全量 480 passed（2026-10-13）；m00679 审查报告九项全过 | 待用户正式验收 |
| C-C-2e-0 真实库只读审计 | 已完成并验证 | 指纹固定、integrity/foreign_key 检查、schema/行数比对、会话日志扫描、C-C-2d 静态审查 | 19:49 写入的进程级来源无法证实（缺日志） | docs/audits/C-C-2e-0-database-safety-audit.md | 等用户人工确认 19:49 来源并裁定保留 |
| m00679 C-C-2d 独立代码审查 | 已完成并验证 | 九项核对全过、零缺陷 P0、风险清单 5 项非阻断、全量测试 480 passed | — | 本窗口补交报告（会话内） | 等用户裁定验收 |
| 真实通知渠道 | 未开始 | —（红线：需独立设计审批） | 全部 | — | 不启动 |
| 通知 HTTP API / 通知中心 / 死信重放 | 未开始 | — | 全部 | — | 后续批次 |
| run/notification 保留清理策略 | 未开始 | — | 全部 | — | 后续批次 |
| P0-1 fencing 测试批次 | **已正式接受（2026-10-14）** | tests/test_reminder_scheduler_fencing.py；7 passed（2026-10-13） | 设计文档矩阵 #7 的跨进程完整 run_tick 端到端用例仍属独立遗留项，不在验收范围 | docs/decisions.md 2026-10-14 条目 | 关闭 |
| P0-2 / P0-3 | **已追认（2026-10-14）** | P0-2：10 秒 heartbeat / 30 秒 TTL / 边界续租 / 失败不伪装成功；P0-3：方案 C（daemon worker + 有界 join + stopped_with_active_tick）+ 35 秒暂定值 | 35 秒未实测校准；进程退出独立验证未完成；超时后同步 SQLite 状态风险未验证 | docs/decisions.md 2026-10-14 条目；代码与 lifecycle 测试 | 后续测试批次补验证 |
| P0-4 | 部分实现，待方案裁定 | `scheduled:{epoch//poll}` 桶键已落地（service `_submit_tick` 与 `run_scheduled_tick`）；同桶首跑幂等（任何状态返回既有记录）；无 catch-up、无 recovery 键 | 桶粒度/时区边界/同桶失败重试/recovery 命名空间/补跑/`tick_once` 默认 run_key 待裁定；桶键双处计算不一致隐患；键格式无直接专项测试 | 2026-10-14 只读核对；候选方案 A/B 对比（会话报告，未批准） | 等用户方案裁定 |

## 4. 已完成工作与实际测试结果

### 4.1 批次 P0-1（此前授权批次）
- 修改文件：`tests/test_reminder_scheduler_fencing.py`（细节属该批次范围，本窗口未重复）。
- 结果：`tests/test_reminder_scheduler_fencing.py` 7 passed（2026-10-13 实跑，2.73s）。
- 未正式验收关闭，等待用户裁定。

### 4.2 C-C-2e-0 真实库只读审计（2026-10-13，零写操作）
- 产出：`docs/audits/C-C-2e-0-database-safety-audit.md`；更新 `docs/current-task.md`、`docs/progress.md`。
- 方法：SQLite URI `mode=ro` + `PRAGMA query_only=ON`；只做元数据/hash/schema/行数/日志静态检查。
- 结果：integrity_check ok、foreign_key_check 0 行、无 sidecar 文件；27 表/35 索引；`reminder_settings` 1 行（全关闭）、`reminders=0`、`reminder_scheduler_runs=0`；无 lease 表、无两张通知表。高置信为 C-C-2c 版本 `init_db()` 幂等初始化，但具体进程/操作者无日志可证实。
- 该轮**未运行 pytest、未启动应用、未写真实库**。

### 4.3 m00679 C-C-2d 独立代码审查（2026-10-13 代码阅读+测试；2026-10-14 报告补交）
- 零文件修改；只读审查 `app/notification_delivery.py`（585 行）、`app/notification_delivery_service.py`（375 行）、`app/reminder_scheduler.py`（375–384 同事务边界）、`app/main.py` lifespan 段、`tests/test_notification_delivery.py`（736 行/28 测试函数）、`tests/conftest.py`。
- 九项核对全部通过（同事务、渠道失败不回滚、幂等键、claim 互斥、退避/死信/恢复、默认关闭+fake 硬限制、关闭语义、脱敏、配置校验先于 init_db）。
- 测试实跑（2026-10-13）：
  - `tests/test_notification_delivery.py`：33 passed, 1 warning（6.52s；warning 为 StarletteDeprecationWarning，httpx/TestClient 引起）
  - `tests/test_reminder_scheduler_fencing.py`：7 passed（2.73s）
  - 全量 `.venv/Scripts/python.exe -m pytest -q`：**480 passed, 1 warning（116.01s）**
- 审查期间出现过一次 3 failed 脏快照（约 20:13），根因是并行会话正在改写 `reminder_scheduler.py`/测试文件；稳定后逐一复现均 1 passed，定性为非缺陷（非当前代码状态结果，勿合并引用）。
- 非阻断风险清单（详见 §6）。
- 结论建议：C-C-2d 实现与测试验收通过；遗留项均为独立后续批次。

### 4.4 历史 C-C-2d 动态验证（早于上述，代码状态不同）
- 曾有全量 `479 passed, 1 skipped, 1 warning` 的记录（docs/progress.md 载明为历史结果，非本轮）。当前最新全量结果为 m00679 的 480 passed。

## 5. 技术决策与设计原因

1. **同事务 outbox**：`scheduled/snoozed→due` 转换与通知 event/delivery 创建在同一 `guarded_write` 事务（`_mark_due_one`，reminder_scheduler.py:375-384）；渠道失败发生在独立 claim→deliver 阶段，不回滚提醒状态。原因：既不丢事件又不因通知失败卡死提醒。
2. **幂等键**：`event_key=reminder.due.v1:{id}:{snoozed_until or scheduled_at}`（区分首次到期与每次 snooze）；`delivery_key={event_key}:{channel}:{recipient}`；应用层 ON CONFLICT DO NOTHING + DB UNIQUE 双重防线。原因：跨连接/跨进程幂等以数据库为权威。
3. **跨进程互斥**：claim 用单条条件 `UPDATE…WHERE id=(SELECT…LIMIT 1) RETURNING *`（BEGIN IMMEDIATE）；有真实 OS 双进程测试。原因：asyncio/threading Lock 不可跨进程。
4. **重试语义**：确定性指数退避（无 jitter，测试锁定）；transient/permanent/timeout 三分类；`not retriable or attempt>=max_attempts → dead_letter`；`recover_stale_deliveries` 保守恢复并明示「可能已外部发送」。jitter 留待真实渠道批次决策。
5. **`succeeded` 语义边界**：仅表示 fake_test 替身成功，不代表真实送达；真实渠道阶段需引入独立 delivered 状态。
6. **默认关闭策略**：两个服务均默认 0；lifespan 中非法配置值在 `init_db()` 之前抛错；service 构造与批处理双重硬校验 channel == `fake_test`。
7. **关闭语义**：`stop()` 有界 `wait_for(asyncio.shield(...))`；超时置 `shutdown_timed_out` 进入 `STATE_STOPPED_ACTIVE_DELIVERY`，不谎报 stopped。
8. **错误脱敏**：`sanitize_error_message` 统一出口。
9. **真实库策略**：来源无法追溯 ≠ 数据库损坏 ≠ 可以安全恢复备份；当前库完整且比备份新，不覆盖不恢复，等人工确认。
10. 以上决策允许在新证据下重新讨论，但改动必须走新批次+用户授权。

## 6. 未解决问题和风险清单

| ID | 问题 | 优先级 | 当前证据 | 影响 | 已尝试的处理 | 下一步 | 需用户裁定 |
|---|---|---|---|---|---|---|---|
| R1 | 19:49:11 真实库写入来源无法证实 | 已裁定（2026-10-14） | 审计报告：形态高置信为 C-C-2c `init_db()`；日志均无线索 | 保留为**历史风险，不视为已解决**；继续禁止写入/初始化/迁移/应用启动 | 用户裁定：保留当前库，来源标记「无法追溯」，不再追查 | 关闭（裁定登记于 docs/decisions.md） | 否（已完成） |
| R2 | `tests/test_reminder_scheduler_fencing.py` 20:10:11 修改来源未知 | P2 | 当前指纹 SHA-256 `01F99BD9F55C1D2408DCEAB08F22E84A7D20F864D8728328D6FD58B89EA465F2`；静态核查安全（tmp_path 临时库）；无 Git 无法还原历史差异 | 无运行时风险；仅来源不明 | 静态审查（已完成） | 如可接受即关闭 | 否（告知即可） |
| R3 | C-C-2d 两张通知表尚未在真实库创建 | P2 | 真实库 27 表，无通知表 | 下次正常 startup 会幂等建表（写真实库） | 记录在案 | 人工知情+先备份后再启动应用 | **是**（未授权） |
| R4 | `succeeded` 语义仅限 fake 渠道 | P2 | 代码注释与 AGENTS.md 一致 | 未来接真实渠道时语义突变 | 文档化 | 真实渠道批次引入独立 delivered 状态 | 否 |
| R5 | `recover_stale_deliveries` 的「可能已发送」窗口 | P2 | 恢复为 retry_waiting 可能重复投递 | fake 场景无害；真实渠道需用 delivery_key 去重 | 已有 delivery_key | 真实渠道批次处理 | 否 |
| R6 | 退避无 jitter | P3 | 测试锁定确定性退避 | 多消费者时同步重试风暴 | 单 worker 场景安全 | 真实渠道批次补决策 | 否 |
| R7 | events/deliveries 表只增不减 | P2 | 无保留/清理代码 | 数据无限增长 | 无 | 与 run 表保留策略同批处理 | 否 |
| R8 | 死信无重放/处置入口 | P2 | dead_letter 为终态，无 API/UI | 运维只能改库 | 无 | 通知 HTTP API 批次 | 否 |
| R9 | `tick_once` 临时 executor 路径未覆盖「stop 期间调用」组合 | P3 | 审查发现，低风险 | 极低 | 建议补测试 | 后续测试批次 | 否 |
| R10 | P0-2/P0-3/P0-4 方案未定 | **大部分已裁定（2026-10-14）** | P0-2/P0-3 已追认（见 §3 表）；P0-4 已恢复全部原始证据（设计文档 §2.2.1/§4/§6.2/§8.3 + 代码核对），现状为部分实现 | 仅 P0-4 桶语义与失败重试规则待裁定 | 只读证据恢复（2026-10-13/14） | P0-4 用户裁定桶粒度/时区/重试/recovery 键/补跑 | **是**（仅 P0-4） |

> 注（2026-10-14 更新）：P0-2/P0-3/P0-4 原始内容已全部从 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md` 与 `docs/decisions.md` 恢复，不再需要用户重述。**事实更正：P0-4 并非完全未实现——`scheduled:{epoch//poll}` 桶键已在 C-C-2c 落地**（此前窗口报告与本文档旧版「service 使用 manual 前缀」的说法有误）。

## 7. 真实数据库专项交接

- 当前指纹（2026-10-14 生成本文档时实测复核）：`49F1FD751F2E6C522A5D319EFBA94FB22EA21A7D6900B2037FB848EBA164F0D8`，352,256 bytes，mtime 2026-10-13 19:49:11.445（+0800）。与审计记录一致，**本窗口未对真实库做任何写操作**。
- 备份：`personal_ai_os.db.bak-20261012-0938-before-1.3.2-A`（163,840 B，2026-10-12）、`personal_ai_os.db.bak-phase2`（49,152 B，2026-10-11）。两者 integrity/foreign_key 均 ok，但都比当前库旧，覆盖有丢数风险，审计结论为不建议。
- 完整性：integrity_check ok、foreign_key_check 0 行、无 WAL/SHM/journal sidecar。
- 内容状态：27 表/35 索引/0 视图/0 触发器；`reminder_settings` 1 行全关闭（行时间=库 mtime）；`reminders=0`；`reminder_scheduler_runs=0`；无 lease 表、无通知表。
- 19:49 调查结论：形态高置信为 C-C-2c 版 `init_db()` 幂等初始化；具体进程/操作者因缺进程与命令日志无法证实。**来源无法追溯 ≠ 数据库损坏，也不等于可以安全恢复备份。**
- 明确没执行过：真实库上的 init_db/迁移/lease/tick/通知 claim、任何写入、任何应用启动。
- 当前允许：只读检查（mode=ro / query_only）。禁止：一切写入、备份覆盖、启动连接真实库的应用。
- 用户尚未裁定：是否「保留当前库」（默认建议保留）。

## 8. C-C-2c 调度器专项交接

- 生命周期：opt-in `ReminderSchedulerService`（`app/reminder_scheduler_service.py`）经 FastAPI lifespan 接入；确定性同步核心在 `app/reminder_scheduler.py`（长 tick 边界续租/停止检查/fencing 写保护）；跨进程互斥权威在 `app/reminder_scheduler_lock.py`（SQLite lease，owner token + fencing token + TTL + stale recovery）。`REMINDER_SCHEDULER_ENABLED` 默认 0。
- P0-1：已正式接受（2026-10-14，范围仅限 fencing 测试批次 7 passed）。
- P0-2：已追认（10 秒 heartbeat / 30 秒 TTL / 边界续租 / 失败不伪装成功）。
- P0-3：已有条件追认（方案 C + 35 秒暂定值）；未决验证：35 秒校准、进程退出独立验证、不杀线程与进程退出取舍、超时后同步 SQLite 状态风险（后续测试批次，未授权修改实现）。
- P0-4：部分实现（`scheduled:{epoch//poll}` 桶键已落地，同桶首跑幂等，无 catch-up/无 recovery 键）；桶语义、同桶失败重试、recovery 命名空间、补跑、`tick_once` 默认 run_key 待裁定；已知隐患：桶键双处计算不一致、键格式无直接专项测试。候选方案 A/B 对比见 2026-10-14 会话报告，未批准。
- 下一步允许：按用户裁定的 P0-4 方案批次处理。禁止：在真实库上启用调度、把自动 tick 描述成 exactly-once 或通知投递、用进程内锁替代 lease。

## 9. C-C-2d 通知投递专项交接

- 实现文件：`app/notification_delivery.py`（events/deliveries 表、幂等键、claim/重试/死信/恢复、fake_test 渠道）、`app/notification_delivery_service.py`（默认关闭 asyncio+单线程消费服务；跨进程领取权威为 SQLite 条件 UPDATE）。
- 事务边界：due 转换 + outbox 同一 guarded transaction（`_mark_due_one`）；渠道失败不回滚提醒状态；有回滚测试。
- 幂等：见 §5-2；有跨连接幂等与 DB 约束双重测试。
- 并发/重试：条件 UPDATE claim + 真 OS 双进程测试；确定性退避；死信终态；stale running 保守恢复。
- fake vs 真实：`succeeded` 仅指 fake_test 替身；无任何真实外部 I/O；`used_network=False`。
- 默认关闭：`NOTIFICATION_DELIVERY_ENABLED=0`；非法值在 init_db 前抛错。
- 尚未实现：通知 HTTP API、通知中心、死信重放、真实渠道（红线需独立审批）、保留策略、jitter。
- 测试：见 §4.3（当前代码状态 33+7+全量 480 passed）。
- 审查状态：m00679 独立审查完成，九项全过、P0 零缺陷；**2026-10-14 用户已有条件验收**（范围与六项限制见 `docs/decisions.md`）。

## 10. 项目整体后续路线图（不自行启动）

依赖顺序（目标/前置/验收标准）：

1. **安全与基线收口**：**已完成（2026-10-14 书面裁定）**——R1 保留当前库、P0-1 接受、C-C-2d 有条件验收、P0-2/P0-3 追认；仅 P0-4 方案仍待裁定（候选 A/B 已研究，未批准）。
2. **真实库知情建表**：备份后在用户知情下启动应用一次，让 C-C-2d 两表幂等创建（R3）。验收：指纹变化与预期一致。
3. **调度器稳定性（P0-2/3/4 批次）**：按用户方案实施+测试。验收：专项+全量绿。
4. **通知 API / 死信重放 / 保留策略**（可合并或分批）。前置：步骤 2。验收：测试+文档。
5. **真实通知渠道**：独立设计审批（凭据、jitter、delivered 状态、渠道侧去重）。前置：步骤 4。
6. **推荐反馈闭环、个人数据接入/同步/分析、个性化建议、自动化审批**：均未开始，按 docs/plans 既有计划推进，前置依赖以上基础设施。

## 11. 当前窗口结束时的精确状态

- 最后一项任务（2026-10-14 批次）：登记第一轮书面裁定（R1/R3、P0-1、C-C-2d、P0-2/P0-3 追认），完成 P0-4 只读候选方案研究，并更新五份授权文档（decisions/progress/current-task/handover/设计文档状态标注）。未修改任何 Python 代码/测试，未写真实库，未启动任何服务。
- 已完成：见 §3/§4 及 `docs/decisions.md` 2026-10-14 条目。全部调查与审查均完成，无悬空实现。
- 尚未验证：真实库通知表建表动作未发生（等待知情授权）；P0-3 四项遗留验证（35 秒校准、进程退出独立验证等）；P0-4 已实现部分无 `scheduled:{bucket}` 键格式专项测试。
- 后台服务：未启动任何 FastAPI/uvicorn/调度/通知服务。
- 等待用户裁定：①P0-4 方案与参数（桶粒度、时区边界、同桶失败重试、recovery 命名空间、停机补跑、`tick_once` 默认 run_key）；②是否安排 P0-3 遗留验证测试批次；③是否授权 R3 知情建表批次（备份后启动应用一次）。
- 新窗口最应该先做：读本文件 → 只读核对（真实库指纹、`docs/current-task.md`、`docs/decisions.md` 2026-10-14 条目）→ 向用户复述 P0-4 待裁定清单 → 停止等待指令。

## 12. 新窗口启动指令（可直接复制）

```
请先完整阅读 docs/handover/current-session-handover.md（项目上下文交接文档）。
读完后：
1. 只做只读核对，不开发：核对 data/personal_ai_os.db 的 SHA-256 是否仍为
   49F1FD751F2E6C522A5D319EFBA94FB22EA21A7D6900B2037FB848EBA164F0D8（大小 352256、
   mtime 2026-10-13 19:49:11），并确认 docs/current-task.md 与交接文档描述一致。
2. 继承 AGENTS.md 全部安全红线与交接文档 §2 的用户长期约束，特别是：不动真实库、
   不启动连接真实库的应用或调度/通知服务、两个 ENABLED 开关保持默认 0、
   分批实施且每批验收后停止等待授权。
3. 不要重复已完成的调查（C-C-2e-0 审计、m00679 审查均已完结），不要因交接文档中的
   路线图或旧计划自动开展任何下一批工作。
4. 核对完成后，向用户复述当前待裁定清单（19:49 真实库保留/恢复；P0-1 是否关闭；
   P0-2/P0-3/P0-4 方案与参数；C-C-2d 是否验收），然后停止，等待用户明确指令。
5. 任何数据库写入、迁移、备份覆盖、后台服务启动，都必须先获得用户对该具体操作的
   明确授权。
```

---

### 文档元信息

- 生成时间：2026-10-14 CST
- 信息覆盖：本窗口全部有效上下文（含 compact 前摘要）、`docs/current-task.md`、`docs/progress.md`、`AGENTS.md`、实测真实库指纹（2026-10-14）。
- 尚未核实/无法确定：P0-2/P0-3/P0-4 具体内容（压缩丢失，见 R10 注）；19:49 写入的进程级来源；fencing 测试文件 20:10:11 修改来源。
- 本文档生成过程未修改任何生产代码、测试、配置或数据库。
