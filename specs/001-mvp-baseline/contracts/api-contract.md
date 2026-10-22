---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_6731c30ec7d111f18f81525400de85a5
    ReservedCode1: wX+DlwxLJ0ZdzN4Sjvuj/4Lffo8CXHJdfz/zFcZCSWAYg1vO4+Tx4bvz398FbHzqUy+n80+at5wmKMgx8wMul8ZQ87fXMX8N++Bg6oZUk6Bp7c2XIjb5Om/zl2GzN2KVi0lQhtZQXX0yHzYIhFnENsot9Ju/+c/oBqdB41Qc7dbUJKqqGAl2OFwU44s=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_6731c30ec7d111f18f81525400de85a5
    ReservedCode2: wX+DlwxLJ0ZdzN4Sjvuj/4Lffo8CXHJdfz/zFcZCSWAYg1vO4+Tx4bvz398FbHzqUy+n80+at5wmKMgx8wMul8ZQ87fXMX8N++Bg6oZUk6Bp7c2XIjb5Om/zl2GzN2KVi0lQhtZQXX0yHzYIhFnENsot9Ju/+c/oBqdB41Qc7dbUJKqqGAl2OFwU44s=
---

# API Contract: Personal AI OS MVP Baseline

**Date**: 2026-10-14
**Feature**: [spec.md](../spec.md) | **Plan**: [plan.md](../plan.md)

## 约定

- 项目为本地 Web 服务，契约形式为 HTTP REST 风格 JSON API，默认监听 `http://127.0.0.1:8000`，仅接受 loopback Host。
- 本文档为基线契约（端点分组与用途），**精确的请求/响应 schema 与错误码以源码路由及测试为准**（见 README 第 9 节）。
- 请求经过 Pydantic 校验，写操作在单个数据库事务中完成，返回 JSON。
- 未配置云端模型 Key 时，依赖模型的端点（记忆候选提取、画像候选生成、建议润色）返回空结果或 400。

## 端点分组

### 系统

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查：`status`、`mode`，含 `reminder_scheduler`、`notification_delivery` 嵌套状态 |

### 个人档案

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/profile` | 读取个人档案 |
| PUT | `/api/profile` | 更新个人档案 |

### 长期记忆

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/memories` | 记忆列表/创建（含审核操作入口） |
| GET | `/api/memory-candidates` | 待审核记忆候选 |
| GET | `/api/memory-insights` | 记忆洞察（重复/冲突/过期提示） |

### 文档知识库

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/documents` | 文档列表 |
| POST | `/api/documents/upload` | 上传文档（PDF/DOCX/MD/TXT，哈希去重） |
| GET | `/api/documents/text` | 获取文档解析文本 |

### 项目 / 目标 / 任务

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/projects` | 项目列表/创建 |
| GET/POST | `/api/goals` | 目标列表/创建 |
| GET/POST | `/api/tasks` | 任务列表/创建 |

### 个人画像

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/profile-items` | 画像条目列表/确认 |

### 连接器

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/connector-types` | 可用连接器类型（local/github/http） |
| GET/POST | `/api/data-sources` | 数据源列表/创建（凭据加密保存） |

### 行为分析

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/behavior/*` | 行为事件记录与查询 |
| GET | `/api/analytics/*` | 确定性分析与周期对比 |

### 建议

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/recommendations` | 建议列表/生成 |
| GET | `/api/recommendation-runs` | 建议生成 Run 记录 |
| POST | `/api/recommendations/*/feedback` | 建议反馈 |
| POST | `/api/recommendations/*/outcome` | 建议结果记录 |

### 对话

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/chat` | 聊天（云端模型或本地演示） |
| GET/POST | `/api/conversations` | 对话记录列表/保存 |

### 审批

| 方法 | 路径 | 用途 |
|---|---|---|
| GET/POST | `/api/approvals` | 审批记录列表/创建 |

## 安全约束（契约级）

- 所有端点仅接受 loopback Host；外部 Host 请求被拒绝。
- 浏览器跨站状态变更请求被防护；模型/文档内容提示注入被隔离。
- 连接器端点禁止访问私网/元数据地址（SSRF 防护）；凭据不出现在响应与日志中。
*（内容由AI生成，仅供参考）*
