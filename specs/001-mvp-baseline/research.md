---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_65564b7fc7d111f19268525400cd780f
    ReservedCode1: yOQDQ3mxdnz1drUejhhMpjiitVzKCTVIkt0+eMsJFXcXDTm7xDLsaHcqDfpqLg4uQyt6whb3CjY7S5pVSbhjr0wAyR/Zh4I1fznT86AegPPuLmd8p4pY8gFPBYr2Bq20ZybURr4D32igzlXSd4NNkkDVvtK5b3Rc/OPLhhQF76Wivfuik1xPFz2Pr9E=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_65564b7fc7d111f19268525400cd780f
    ReservedCode2: yOQDQ3mxdnz1drUejhhMpjiitVzKCTVIkt0+eMsJFXcXDTm7xDLsaHcqDfpqLg4uQyt6whb3CjY7S5pVSbhjr0wAyR/Zh4I1fznT86AegPPuLmd8p4pY8gFPBYr2Bq20ZybURr4D32igzlXSd4NNkkDVvtK5b3Rc/OPLhhQF76Wivfuik1xPFz2Pr9E=
---

# Research: Personal AI OS MVP Baseline

**Date**: 2026-10-14
**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

## 说明

本特性为基线规格（描述当前已实现完整功能），Technical Context 中不存在未确认项（无 NEEDS CLARIFICATION），因此 Phase 0 研究聚焦于对现状技术方案的确认决策与替代方案回顾，为后续迭代提供依据。

## 1. 应用形态与交付方式

- **Decision**: 本地优先的单用户 Web 服务；后端 HTTP API + 无构建步骤的原生浏览器前端；数据存于本机。
- **Rationale**: MVP 面向个人本地使用，无需登录/多租户，原生前端可零构建直接演示；默认不配置云端模型 Key 也能完整运行核心功能。
- **Alternatives considered**: 桌面壳应用（Electron/Tauri）——引入打包与分发复杂度，当前无必要；纯 CLI——失去可视化档案/记忆/文档管理体验；托管云服务——违反本地优先与数据隐私约束。

## 2. 数据存储

- **Decision**: 本机 SQLite 单文件数据库，应用启动时执行幂等建表与增量迁移。
- **Rationale**: 单用户、单机、数据量小，SQLite 提供事务、跨进程租约能力与零运维成本；测试通过临时数据库隔离真实数据。
- **Alternatives considered**: PostgreSQL/pgvector——为向量检索预留替换接缝但当前未迁移；文件型存储——无法满足结构化记录、状态机与并发语义；内存存储——不可持久化。

## 3. 云端模型集成

- **Decision**: 通过 OpenAI 兼容 Chat Completions API 提供可选增强（聊天、记忆候选提取、画像候选生成、建议润色），默认不启用。
- **Rationale**: 未配置 Key 时核心功能不受影响；模型只作为增强层，数字事实由确定性规则生成，防止模型幻觉污染个人数据。
- **Alternatives considered**: 本地模型推理——资源开销大且当前无需；硬依赖云端——违反无 Key 演示约束；完全确定性——失去自然语言处理价值。

## 4. 外部数据整合

- **Decision**: 提供本地目录、GitHub 单仓库、通用 HTTP GET API 三类只读连接器，逐源 opt-in 定时同步；凭据 Fernet 加密存储。
- **Rationale**: 只读拉取最小化安全暴露；三类来源覆盖个人文件、代码仓库与在线服务；加密凭据避免明文密钥进入普通配置。
- **Alternatives considered**: 双向/写入连接器——超出 MVP 安全边界；全量自动同步所有源——违反 opt-in 原则与资源约束；明文配置——违反安全红线。

## 5. 提醒与通知

- **Decision**: 提醒调度接入应用生命周期但默认关闭；到期通知采用事件与投递记录分离的 outbox 模型；当前仅内置 `fake_test` 测试渠道，真实外部通知未实现。
- **Rationale**: 完整实现调度核心、跨进程租约、幂等去重与重试/死信语义，同时以开关默认关闭避免未经验证的通知打扰用户；at-least-once 语义被明确接受。
- **Alternatives considered**: exactly-once 通知——分布式事务开销大且与 SQLite 单机模型不匹配；即时同步发送——无法提供投递可追溯性；真实渠道优先——违反安全渐进原则。

## 6. 安全边界

- **Decision**: 仅 loopback 访问；校验 Host 头防止 DNS rebinding；防护跨站状态变更；隔离模型/文档内容提示注入；连接器禁私网/元数据地址。
- **Rationale**: 无登录体系下，绑定本机是唯一安全部署方式；提示注入与 SSRF 防护防止恶意文档/连接器影响系统。
- **Alternatives considered**: 引入完整认证体系——超出 MVP 范围，应作为独立特性；公网部署——当前架构不允许。

## 7. 测试策略

- **Decision**: pytest 全量测试（基线 445 passed, 1 skipped）；纯函数直接单测、API 用 TestClient+临时库、外部网络 mock、并发语义用真实 OS 子进程验证。
- **Rationale**: 关键领域（提醒调度、租约、通知 outbox）的正确性依赖真实并发环境，子进程测试能验证跨进程互斥与 fencing。
- **Alternatives considered**: 仅 mock 单测——无法验证 SQLite 跨进程语义；集成环境测试——需要部署目标环境，违反本地优先。

## 结论

所有技术决策均确认现状方案为当前条件下的合理基线；后续迭代如需变更（如引入向量检索、真实通知渠道、认证体系），应以本文件记录的理由为起点评估。
*（内容由AI生成，仅供参考）*
