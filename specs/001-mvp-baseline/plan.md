---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_64129c4cc7d111f19268525400cd780f
    ReservedCode1: 6dPYGvEufiNOIeCYDLJIK6txLQRroMl/1Mf1nRi2kCTFqe2NFIOljBIED992JHXmGLf1kl6Gfch8ECIFe7/n+GgjGBNDUzNBEJ/+BzcvPi+GLiv9ucWTH+nF06uopwqHvMnKIbAQI0x5Z06woV3wReUIq1iTR/Yuo/lR1LgOlcin46BVrn46Qj91TCs=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_64129c4cc7d111f19268525400cd780f
    ReservedCode2: 6dPYGvEufiNOIeCYDLJIK6txLQRroMl/1Mf1nRi2kCTFqe2NFIOljBIED992JHXmGLf1kl6Gfch8ECIFe7/n+GgjGBNDUzNBEJ/+BzcvPi+GLiv9ucWTH+nF06uopwqHvMnKIbAQI0x5Z06woV3wReUIq1iTR/Yuo/lR1LgOlcin46BVrn46Qj91TCs=
---

# Implementation Plan: Personal AI OS MVP Baseline

**Branch**: `001-mvp-baseline` | **Date**: 2026-10-14 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-mvp-baseline/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

本特性为 **Personal AI OS MVP 基线规格**：以当前已实现的完整功能（本机个人工作台、长期记忆、文档知识库、项目/目标/任务记录、行为分析与画像建议闭环、只读连接器、提醒与通知 outbox、本机安全防护）为基线，固化现有设计与接口契约，作为后续迭代的基准。技术方案采用本地优先的单用户 Web 服务形态：后端提供 HTTP API，前端为无构建步骤的原生浏览器界面，数据持久化于本机数据库。本 plan 的目标是沉淀基线设计产物（技术上下文、数据模型、API 契约、快速验证指南），本身不引入新功能实现。

## Technical Context

**Language/Version**: Python 3.11+（当前开发环境实测 Python 3.13.9）

**Primary Dependencies**: FastAPI + Uvicorn、Pydantic、httpx、pypdf、python-docx、cryptography/Fernet

**Storage**: 本机 SQLite（默认 `data/personal_ai_os.db`，可用 `DATABASE_PATH` 覆盖）

**Testing**: pytest（当前基线 `445 passed, 1 skipped`）；测试强制临时数据库/密钥隔离，外部网络通过 mock transport 或 DNS 打桩

**Target Platform**: 本机运行（默认 loopback `127.0.0.1:8000`）；Windows 启动脚本 `run.bat`，开发环境支持热重载

**Project Type**: 本地优先的单用户 web-service（无构建步骤的原生 HTML/CSS/JavaScript 前端 + HTTP API）

**Performance Goals**: 单用户本地使用；规格要求记忆/文档检索结果在 2 秒内返回（SC-003）；同步 tick 软上限 90 秒，关闭等待上限 35 秒

**Constraints**: 仅 loopback 访问（无登录/多用户）；未配置云端模型 Key 时以本地演示逻辑运行；SQLite 单机方案不承诺多机共享与 exactly-once；自动提醒调度与通知消费默认关闭；真实外部通知未实现

**Scale/Scope**: 单用户、本地数据、MVP 规模（约 20+ 个后端领域模块、80+ 路由、原生前端页面）；上传原始文件不以独立磁盘文件长期保存

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- `.specify/memory/constitution.md` 当前为 spec-kit 占位模板，未填充自定义原则（`[PRINCIPLE_1_NAME]` 等均为占位符），因此无自定义 gate 约束。
- 依据 spec-kit 默认质量 gate 评估基线规格与设计产物：① 规格聚焦用户价值与业务需求，无实现细节泄露（模板章节完整、成功标准可测量且技术无关）；② 数据模型与契约基于现有代码与文档沉淀，不存在虚构接口；③ 快速验证指南中的命令全部来自项目已存在入口。
- **Gate 结果：PASS**（无违反，无需 Complexity Tracking 豁免）。

## Project Structure

### Documentation (this feature)

```text
specs/001-mvp-baseline/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   └── api-contract.md  # HTTP API 接口契约基线
├── checklists/
│   └── requirements.md  # 规格质量检查清单（/speckit-specify 产出）
└── spec.md              # 基线规格（/speckit-specify 产出）
```

### Source Code (repository root)

```text
personal-ai-os-mvp/
├── app/
│   ├── main.py                # FastAPI 组装、lifespan、中间件、路由、schema 与迁移
│   ├── model_config.py        # OpenAI 兼容模型配置
│   ├── memory_extract.py      # 记忆候选提取
│   ├── memory_match.py        # 记忆相似度/重复/冲突判断
│   ├── retriever.py           # 记忆/文档片段/工作项检索
│   ├── document_parse.py      # 文档字节解析（PDF/DOCX/MD/TXT）
│   ├── document_chunk.py      # 文本清洗、分块、来源定位
│   ├── behavior_events.py     # 行为事件记录
│   ├── analytics.py           # 确定性分析
│   ├── profile_propose.py     # 画像候选生成
│   ├── recommendations.py     # 建议存储/状态机
│   ├── recommendation_engine.py  # 证据收集与规则引擎
│   ├── recommendation_feedback.py # 建议反馈/结果
│   ├── recommendation_stats.py    # 建议有效性统计
│   ├── connectors.py          # connector 基类与注册中心
│   ├── github_connector.py    # GitHub 只读 connector
│   ├── httpapi_connector.py   # 通用 HTTP API 只读 connector
│   ├── connector_scheduler.py # 数据源同步 asyncio 循环
│   ├── secret_crypto.py       # 凭据 Fernet 加密
│   ├── reminders.py           # 提醒领域、规则、状态机
│   ├── reminder_scheduler.py  # 确定性同步 tick、fencing 写入
│   ├── reminder_scheduler_service.py # opt-in asyncio 生命周期
│   ├── reminder_scheduler_lock.py    # SQLite 跨进程租约
│   ├── notification_delivery.py      # 通知 outbox、重试、fake 渠道
│   ├── notification_delivery_service.py # 默认关闭的 fake 消费服务
│   └── static/                # 原生前端（无构建步骤）
├── tests/
│   ├── conftest.py            # 强制临时数据库/密钥隔离
│   └── test_*.py
├── data/                      # 本地运行数据（默认不提交）
├── docs/                      # 架构/模块职责/数据库/配置等文档
├── .specify/                  # spec-kit 配置与模板
└── specs/                     # 特性目录（本特性 001-mvp-baseline）
```

**Structure Decision**: 采用项目现有"Fat entrypoint + 领域扁平模块"结构（Option 1 单项目形态的现状演进）：`app/main.py` 承担 FastAPI 组装、路由、schema 与迁移，领域模块按业务边界独立可测；前端 `app/static/` 只调用 HTTP API。本 plan 不改变现有目录结构，仅沉淀设计基线。后续如需拆分 `app/main.py`，应在 API 契约保护下按领域小步提取（见 architecture.md 已知限制 P1）。

## Complexity Tracking

> 本表仅当 Constitution Check 存在需豁免的违反时填写；当前 Gate 结果为 PASS，无违反，无需豁免。
*（内容由AI生成，仅供参考）*
