---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_664835a7c7d111f19268525400cd780f
    ReservedCode1: OR6GOnDkn7Av1FWflpMwk/gZ6cPdAkF44AMLlP10yHWBziBFpZ82pE49O2ecZ+MDv8ZALxY9idaQ4zmXdmG4jfDi2V43pcnyNSQ1ugPyVYn7OUd7brj5dom0A9o+rYJ+677QaKdipQHGN7GJeaDDEsfHT5GPqlQW+DWw31WE8lO2rT/gJZnVRfJlWug=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_664835a7c7d111f19268525400cd780f
    ReservedCode2: OR6GOnDkn7Av1FWflpMwk/gZ6cPdAkF44AMLlP10yHWBziBFpZ82pE49O2ecZ+MDv8ZALxY9idaQ4zmXdmG4jfDi2V43pcnyNSQ1ugPyVYn7OUd7brj5dom0A9o+rYJ+677QaKdipQHGN7GJeaDDEsfHT5GPqlQW+DWw31WE8lO2rT/gJZnVRfJlWug=
---

# Data Model: Personal AI OS MVP Baseline

**Date**: 2026-10-14
**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

## 说明

本数据模型为基线设计，实体与关系基于现有代码与模块职责沉淀（见 `docs/module-responsibilities.md`、`docs/database.md`）。字段描述采用业务语义概括，精确 schema 以应用启动幂等建表为准；本模型用于指导后续迭代，不替代实际迁移。

## 实体总览

| 实体 | 说明 | 关键关系 |
|---|---|---|
| Profile | 个人档案，数据锚点 | 1:1 系统基础数据 |
| Memory | 长期记忆（含候选状态） | 引用来源关联 Document/Conversation |
| Document | 文档知识库条目 | 1:N Chunk |
| Chunk | 文档分块片段 | N:1 Document |
| Project / Goal / Task | 工作流结构化记录 | Project 1:N Goal；Goal 1:N Task |
| Conversation | 对话记录 | 1:N Message（输入记忆提取/行为分析） |
| Approval | 审批记录 | 独立追溯记录 |
| BehaviorEvent | 只追加行为事件 | 事件流，供 Analytics 聚合 |
| ProfileItem | 画像条目 | 由证据生成，供用户确认 |
| Recommendation | 证据驱动建议 | 1:N Feedback/Outcome；关联 Run 记录 |
| DataSource | 只读连接器配置 | 1:N SyncLog |
| ConnectorSecret | 加密凭据 | 与 DataSource 关联，Fernet 加密 |
| Reminder | 提醒（设置/规则/状态） | 状态机推进，到期生成 NotificationEvent |
| NotificationEvent | 通知业务事实 | 1:N NotificationDelivery |
| NotificationDelivery | 渠道投递生命周期 | N:1 NotificationEvent |

## 实体详情

### Profile（个人档案）
- **字段**: 标识、基本信息字段（如姓名/偏好等，随 UI 演进）、更新时间
- **校验**: 单用户唯一档案；更新幂等
- **关系**: 独立基础数据，其他领域（记忆/画像/建议）以其为上下文

### Memory（长期记忆）
- **字段**: 内容、状态（candidate/pending/approved/rejected/expired 等）、来源引用、重复/冲突标记、修订历史、过期策略
- **校验规则**: 候选须经用户审核方可成为正式记忆；重复或冲突候选须提示用户
- **状态转换**: `candidate -> approved | rejected`；`approved -> expired`；修订保留历史版本

### Document（文档）与 Chunk（分块）
- **字段（Document）**: 名称、类型、内容哈希、解析文本/元数据、上传时间
- **字段（Chunk）**: 所属文档、文本片段、来源页码/标题、顺序
- **校验规则**: 内容哈希用于识别重复文档（FR-009）；支持 PDF/DOCX/MD/TXT（FR-008）
- **关系**: Document 1:N Chunk；Chunk 供检索返回来源定位（FR-007）

### Project / Goal / Task
- **字段**: 名称/标题、描述、状态、截止/时间、层级关联
- **校验规则**: 任务可关联目标与项目；支持创建、更新与查看（FR-010）
- **关系**: Project 1:N Goal；Goal 1:N Task；可被检索（retriever 工作项检索）

### Conversation（对话）与 Approval（审批）
- **字段（Conversation）**: 消息内容、时间、角色；**字段（Approval）**: 事项、结论、时间
- **校验规则**: 可保存与追溯（FR-011）
- **关系**: Conversation 是记忆候选提取与行为分析的输入源

### BehaviorEvent（行为事件）
- **字段**: 事件类型、时间、脱敏负载（不存标题/正文/密钥）
- **校验规则**: 只追加（append-only）；可回填；不存敏感内容
- **关系**: 事件流输入 Analytics 与 Recommendation 证据

### ProfileItem（画像条目）
- **字段**: 内容、状态（候选/已确认）、证据引用、生成时间
- **校验规则**: 候选须经用户确认；云端生成仅产生 pending 候选不直接写库

### Recommendation（建议）与 Feedback/Outcome
- **字段**: 建议内容、证据、状态、冷却/去重键、Run 记录
- **校验规则**: 数字事实由确定性规则生成，模型仅可选润色；反馈/结果受状态约束；统计为只读
- **状态转换**: `generated -> feedback/outcome 记录`；去重与冷却防止重复建议

### DataSource（数据源）与 SyncLog（同步日志）
- **字段**: 类型（local/github/http）、配置（凭据加密引用）、opt-in 开关、同步状态
- **校验规则**: 只读拉取；逐源 opt-in 定时同步；同步失败不影响其他源（FR-016）
- **关系**: DataSource 1:N SyncLog；凭据经 ConnectorSecret 加密保存（FR-017）

### Reminder（提醒）
- **字段**: 触发时间、规则、安静时段、状态机状态、去重键
- **校验规则**: 支持创建/规则/安静时段（FR-018）；到期生成唯一通知事件（FR-019）
- **状态转换**: `scheduled -> due`（含 snoozed 周期）；重复 tick 不重复插入（幂等）

### NotificationEvent 与 NotificationDelivery（通知事件/投递）
- **字段（Event）**: 业务事实标识（稳定幂等键）、到期时机；**字段（Delivery）**: 渠道、状态（pending/running/succeeded/retry_waiting/dead_letter）、退避与重试次数
- **校验规则**: 事件与投递分离，不可混为"已送达"；投递领取原子完成；失败进入重试/死信；stale running 保守恢复（FR-020）
- **状态转换**: `pending -> running -> succeeded`；`running -> retry_waiting -> dead_letter`（达 5 次上限）；`running -> pending`（stale 恢复）

## 一致性约束（基线）

- 提醒到期与通知 outbox 在同一事务内完成，保证不丢失到期事实。
- 跨进程场景下提醒调度由租约与 fencing token 保护（FR-022），防止重复执行。
- 连接器凭据仅以加密形式存储（FR-017），日志与普通配置禁止明文密钥。
- 测试使用临时数据库，绝不写入真实开发库。
*（内容由AI生成，仅供参考）*
