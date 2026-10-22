---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_682bc11dc7d111f19268525400cd780f
    ReservedCode1: VG7fmPjgBPMSfIFNoGIq4SbL9iErNiiqLmy8m0CKYZw8U0yHHol1AwLBE63tPDWoy44zIBAHCR3RpZTjpgZNMBtXzvupwTwP7YML2uHy8wR7x/+GZhp+7BfQpnemngYfuasfzYid5ac3F3bnXH27RewLtbNxorEtWNFjXvmYd5x64yXHA+dBdmklBXI=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_682bc11dc7d111f19268525400cd780f
    ReservedCode2: VG7fmPjgBPMSfIFNoGIq4SbL9iErNiiqLmy8m0CKYZw8U0yHHol1AwLBE63tPDWoy44zIBAHCR3RpZTjpgZNMBtXzvupwTwP7YML2uHy8wR7x/+GZhp+7BfQpnemngYfuasfzYid5ac3F3bnXH27RewLtbNxorEtWNFjXvmYd5x64yXHA+dBdmklBXI=
---

# Quickstart: Personal AI OS MVP Baseline 快速验证指南

**Date**: 2026-10-14
**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Contracts**: [api-contract.md](contracts/api-contract.md) | **Data Model**: [data-model.md](data-model.md)

## 目的

本指南提供可运行的端到端验证场景，证明基线规格（FR-001 ~ FR-023）所描述的功能在当前实现中真实可用。它作为验证/运行指南，不含实现代码；实现细节归属 tasks.md 与实现阶段。

## 前置条件

- 项目根目录 `D:\develop\VibeCodingProjects\personal-ai-os-mvp`，已安装依赖（`requirements.txt` + `requirements-dev.txt`）。
- 可选：复制 `.env.example` 为 `.env`（不配置云端模型 Key 也可验证核心功能）。
- 数据库：默认 `data/personal_ai_os.db`；验证建议使用临时副本或空库，避免污染真实开发数据。

## 启动

```powershell
# 方式一（推荐）：Windows 启动脚本
.\run.bat

# 方式二：手动启动（普通会话需先设置环境变量，或仅本地演示模式）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**预期结果**：服务监听 `http://127.0.0.1:8000`；`GET /api/health` 返回 `status: ok` 且 `mode` 字段正常。

## 测试基线

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests   # 静态编译
.\.venv\Scripts\python.exe -m pytest -q                 # 全量测试
```

**预期结果**：`445 passed, 1 skipped, 1 warning`（基线）；测试强制使用临时数据库，不触碰真实库。

## 端到端验证场景

### 场景 1：本机工作台与档案管理（FR-001, FR-002, SC-001）

1. 浏览器打开 `http://127.0.0.1:8000`，看到个人档案界面。
2. 修改档案字段并保存；刷新页面后修改仍在（持久化）。
3. 不配置云端模型 Key，使用聊天入口得到本地演示回应。

**验证命令**：`Invoke-RestMethod http://127.0.0.1:8000/api/health`；`Invoke-RestMethod -Method Get http://127.0.0.1:8000/api/profile`

### 场景 2：长期记忆闭环（FR-003 ~ FR-007, SC-003）

1. 产生对话内容后，访问 `/api/memory-candidates` 查看候选。
2. 审核（批准/拒绝）候选；批准项进入 `/api/memories`。
3. 构造重复/冲突候选，验证系统给出提示。
4. 输入关键词检索记忆，结果附带引用来源。

**验证命令**：`Invoke-RestMethod http://127.0.0.1:8000/api/memories`；`Invoke-RestMethod http://127.0.0.1:8000/api/memory-candidates`

### 场景 3：文档知识库（FR-008, FR-009, FR-007, SC-002, SC-004）

1. 上传 PDF/DOCX/MD/TXT 各一份，`/api/documents` 出现对应条目。
2. 重复上传同一文件，条目不重复增加（哈希去重）。
3. 检索关键词，返回片段与来源页码/标题。

**验证命令**：`Invoke-RestMethod http://127.0.0.1:8000/api/documents`

### 场景 4：项目/目标/任务与记录（FR-010, FR-011）

1. 创建项目并关联目标与任务；列表可见且可更新。
2. 保存一次对话与审批记录，可再次检索。

**验证命令**：`Invoke-RestMethod http://127.0.0.1:8000/api/projects`；`Invoke-RestMethod http://127.0.0.1:8000/api/approvals`

### 场景 5：行为分析、画像与建议闭环（FR-012 ~ FR-014）

1. 产生行为事件后查看 `/api/behavior/*` 与 `/api/analytics/*` 分析结果与周期对比。
2. 查看 `/api/profile-items` 画像条目与 `/api/recommendations` 建议（含证据）。
3. 对建议提交 feedback 与 outcome。

### 场景 6：只读连接器（FR-015 ~ FR-017, SC-005）

1. 添加本地目录/GitHub/HTTP API 数据源，执行一次同步并查看同步日志。
2. 对某源开启定时同步，验证到点自动同步且不干扰其他源。
3. 配置含凭据的数据源，验证配置中无明文密钥（加密保存）。

### 场景 7：提醒与通知 outbox（FR-018 ~ FR-021, SC-006）

1. 创建带触发时间的提醒；手动触发 `run_tick()` 推进状态到 due。
2. 验证到期生成唯一通知事件（重复触发不重复插入）。
3. 启用 `fake_test` 渠道消费（`NOTIFICATION_DELIVERY_ENABLED=1`），验证投递记录推进与失败重试/死信。
4. 默认不启用自动调度：启动应用后验证无自动 tick 运行。

### 场景 8：安全防护（FR-022, FR-023, SC-008）

1. 使用非 loopback Host 访问，验证请求被拒绝。
2. 并发/多进程启动时验证提醒调度不重复执行（SQLite 租约与 fencing）。
3. 上传含提示注入内容的文档/模型输入，验证不影响系统行为。

## 预期总体结果

| 验证项 | 预期 |
|---|---|
| 全量测试 | 445 passed, 1 skipped |
| 健康检查 | status ok |
| 档案持久化 | 重启后仍可访问 |
| 重复文档 | 100% 跳过（SC-004） |
| 通知事件 | 100% 唯一（SC-006） |
| 外部访问 | 100% 拒绝（SC-008） |
*（内容由AI生成，仅供参考）*
