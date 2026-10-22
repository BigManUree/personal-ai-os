---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_6d082b31c7d811f18f81525400de85a5
    ReservedCode1: FnXrz73QjcnVnELj817279oc+2sCKAF4A8uEVYsHvnhiihYvM0plWPgDNMOPFA6arq44DUjvRq1WBxq8nYGH9CTEbCIPHGpr+jHeZr7Hjju1e52bM8epCeLewKBU+aMj/Wsn0CAXErI09aW53SvWLyckNX9irzbzIuJDPtEz+FbkAaKCNmbDa2viNk4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_6d082b31c7d811f18f81525400de85a5
    ReservedCode2: FnXrz73QjcnVnELj817279oc+2sCKAF4A8uEVYsHvnhiihYvM0plWPgDNMOPFA6arq44DUjvRq1WBxq8nYGH9CTEbCIPHGpr+jHeZr7Hjju1e52bM8epCeLewKBU+aMj/Wsn0CAXErI09aW53SvWLyckNX9irzbzIuJDPtEz+FbkAaKCNmbDa2viNk4=
---



# Tasks: Personal AI OS MVP Baseline

**Input**: Design documents from `/specs/001-mvp-baseline/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: 测试任务为 OPTIONAL，仅当特性规格显式要求时才包含。本基线规格未显式要求测试任务；已有测试基线集中收录于 Phase 10（全量 `445 passed, 1 skipped`）。各用户故事任务均以 `tests/test_*.py` 对应文件作为完成判据之一。

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions
- **[x] / [ ]**: 任务状态标注——基线特性为当前已实现能力，已完成任务勾选 `[x]` 并列出对应实现文件；待实现/待执行任务保持 `[ ]`

## Path Conventions

- **Single project**: `src/`, `tests/` at repository root（本项目采用单项目形态：`app/`、`tests/` 位于仓库根）

---

## Phase 1: Setup (Shared Infrastructure)

- [x] **T001** **Setup 项目骨架与依赖清单**：确认 `pyproject.toml`、`requirements.txt`、`requirements-dev.txt`、`run.bat` 存在并指向 FastAPI/Uvicorn/Pydantic/httpx/pypdf/python-docx/cryptography 依赖。
- [x] **T002** **Setup 本地运行环境**：确认本机 Python 3.13.9 可运行项目，`data/` 目录可写入（默认 `data/personal_ai_os.db`，`DATABASE_PATH` 可覆盖）。
- [x] **T003** **[P] Setup 开发质量基础设施**：确认 `pytest.ini`/`pyproject.toml` 中 pytest 配置、`tests/conftest.py` 强制临时数据库/密钥隔离生效。

## Phase 2: Foundational (Blocking Prerequisites)

- [x] **T004** **Foundational 数据库 schema 与幂等初始化**：`app/main.py` 中 `init_db()` 建表并执行幂等迁移，覆盖 15 个实体（见 data-model.md）。
- [x] **T005** **[P] Foundational API 组装与中间件**：`app/main.py` 组装 FastAPI lifespan、路由注册、异常处理与日志基础设施。
- [x] **T006** **[P] Foundational 环境与模型配置**：`app/model_config.py` 提供 OpenAI 兼容模型配置；`.env.example` 说明 `DATABASE_PATH` 等可覆盖项。
- [x] **T007** **[P] Foundational 本机安全防护**：仅监听 loopback（默认 `127.0.0.1:8000`）、Host 校验拒绝外部访问、跨站状态变更防护与提示注入防护（对应 FR-023 / SC-008）。
- [x] **T008** **[P] Foundational 领域模块化边界**：后端领域模块按业务边界独立（见 plan.md Project Structure 的 `app/*.py` 清单），供后续用户故事任务复用。

## Phase 3: US1 - 本机个人工作台与档案管理 (P1)

- [x] **T009** **[P] [US1] 个人档案存储与读写端点**：`app/main.py` 中 `/api/profile` 相关路由支持档案查看与更新，持久化于本机数据库（FR-001 / SC-001）。
- [x] **T010** **[P] [US1] 原生前端工作台页面**：`app/static/` 提供无构建步骤的浏览器界面，展示个人档案并调用 HTTP API（FR-001）。
- [x] **T011** **[US1] 聊天入口与本地降级**：`app/main.py` 中 `/api/chat` 未配置云端模型 Key 时以本地演示逻辑回应（FR-002 / SC-007）。

## Phase 4: US2 - 长期记忆管理 (P1)

- [x] **T012** **[P] [US2] 记忆候选提取**：`app/memory_extract.py` 从对话内容生成待审核候选（FR-003）；未配置云端 Key 时降级为空结果。
- [x] **T013** **[P] [US2] 记忆相似度/重复/冲突判断**：`app/memory_match.py` 比对候选与已有记忆并提示重复/冲突（FR-005）。
- [x] **T014** **[P] [US2] 记忆检索**：`app/retriever.py` 支持关键词检索记忆并附带引用来源（FR-007 / SC-003，检索 2 秒内返回）。
- [x] **T015** **[US2] 记忆审核、过期与修订**：`app/main.py` 中 `/api/memories*` 支持候选批准/拒绝批量审核、过期管理与修订历史（FR-004 / FR-006）。

## Phase 5: US3 - 文档知识库 (P1)

- [x] **T016** **[P] [US3] 文档解析**：`app/document_parse.py` 解析 PDF/DOCX/Markdown/纯文本为文本内容（FR-008）。
- [x] **T017** **[P] [US3] 文本分块与来源定位**：`app/document_chunk.py` 清洗、分块并记录来源页码/标题（FR-007 / FR-008）。
- [x] **T018** **[US3] 文档上传、去重与检索端点**：`app/main.py` 中 `/api/documents*` 支持上传、内容校验去重（100% 识别重复文档，FR-009 / SC-004）与片段检索（FR-007 / SC-002）。

## Phase 6: US4 - 项目、目标、任务、对话与审批记录 (P2)

- [x] **T019** **[P] [US4] 项目/目标/任务记录端点**：`app/main.py` 中 `/api/projects`、`/api/goals`、`/api/tasks` 支持创建、更新与查看（FR-010）。
- [x] **T020** **[P] [US4] 对话记录端点**：`app/main.py` 中 `/api/conversations` 保存与追溯对话记录（FR-011）。
- [x] **T021** **[P] [US4] 审批记录端点**：`app/main.py` 中 `/api/approvals` 保存与追溯审批记录（FR-011）。
- [x] **T022** **[US4] 工作项检索集成**：`app/retriever.py` 的 `search_work_items` 将项目/目标/任务纳入检索（FR-010 / FR-011）。

## Phase 7: US5 - 行为分析、画像与建议反馈闭环 (P2)

- [x] **T023** **[P] [US5] 行为事件记录**：`app/behavior_events.py` 只追加记录用户行为事件（FR-012）。
- [x] **T024** **[P] [US5] 确定性分析**：`app/analytics.py` 提供确定性统计结果与周期对比（FR-012）。
- [x] **T025** **[P] [US5] 画像候选生成**：`app/profile_propose.py` 由证据生成个人画像条目（FR-013）；未配置云端 Key 时降级。
- [x] **T026** **[P] [US5] 建议引擎与反馈闭环**：`app/recommendation_engine.py`、`app/recommendations.py`、`app/recommendation_feedback.py`、`app/recommendation_stats.py` 生成证据驱动建议并支持反馈/结果/有效性统计（FR-013 / FR-014）。
- [x] **T027** **[US5] 建议端点**：`app/main.py` 中 `/api/recommendations*` 暴露建议列表、证据说明、反馈提交（FR-013 / FR-014）。

## Phase 8: US6 - 只读数据源连接与定时同步 (P2)

- [x] **T028** **[P] [US6] connector 基类与注册中心**：`app/connectors.py` 定义只读 connector 抽象与注册（FR-015）。
- [x] **T029** **[P] [US6] GitHub 只读 connector**：`app/github_connector.py` 拉取 GitHub 仓库只读条目（FR-015）。
- [x] **T030** **[P] [US6] HTTP API 只读 connector**：`app/httpapi_connector.py` 拉取通用 HTTP API 条目（FR-015；本地目录源由 `app/connectors.py` 覆盖）。
- [x] **T031** **[P] [US6] 凭据加密存储**：`app/secret_crypto.py` 以 Fernet 加密连接器凭据，普通配置不出现明文（FR-017）。
- [x] **T032** **[P] [US6] 定时同步调度**：`app/connector_scheduler.py` 逐源选择定时同步并记录同步日志（FR-016 / SC-005，单源失败不影响其他源）。
- [x] **T033** **[US6] 数据源端点**：`app/main.py` 中 `/api/connector-types`、`/api/data-sources` 支持数据源配置、手动同步与日志查看（FR-015 / FR-016）。

## Phase 9: US7 - 提醒与到期通知（测试渠道） (P3)

- [x] **T034** **[P] [US7] 提醒领域与状态机**：`app/reminders.py` 支持提醒创建、规则、安静时段与状态推进（FR-018）。
- [x] **T035** **[P] [US7] 同步 tick 调度与 fencing 写入**：`app/reminder_scheduler.py` 确定性 tick 推进到期提醒，fencing 写入避免重复（FR-019 / FR-022 / SC-006）。
- [x] **T036** **[P] [US7] 跨进程租约**：`app/reminder_scheduler_lock.py` 基于 SQLite 的跨进程租约，保证并发/多实例不重复执行（FR-022）。
- [x] **T037** **[P] [US7] 通知 outbox 与投递生命周期**：`app/notification_delivery.py` 实现事件与投递分离、唯一通知事件、重试/指数退避/死信/运行中记录恢复，当前仅内置测试渠道（FR-019 / FR-020）。
- [x] **T038** **[P] [US7] 生命周期服务（默认关闭）**：`app/reminder_scheduler_service.py`、`app/notification_delivery_service.py` 未显式启用时不运行（FR-021）。
- [x] **T039** **[US7] 提醒端点**：`app/main.py` 中提醒相关路由支持创建/规则/安静时段与手动触发状态推进（FR-018 / US7 Independent Test）。

## Phase 10: Polish & Cross-Cutting Concerns

- [x] **T040** **[P] 全量测试基线验证**：确认测试基线 `445 passed, 1 skipped`（外部网络通过 mock transport / DNS 打桩隔离）。
- [x] **T041** **[P] 运行 quickstart.md 端到端验证**：已按 `quickstart.md` 完成验证（2026-10-14）：
  - 启动：临时库 `DATABASE_PATH=temp/e2e_verify.db` 启动 uvicorn（127.0.0.1:8010，8000 被外部进程占用），真实 `data/personal_ai_os.db` 未触碰（其最后修改时间早于本轮验证窗口）；
  - 档案读写：PUT/GET `/api/profile` 成功，中文 UTF-8 存储无乱码；
  - 文档上传检索：POST `/api/documents/upload` 上传 md/txt 成功（chunk_count=1），重复上传返回 409 去重，GET `/api/documents/{id}` 可查分块；
  - 记忆审核：POST `/api/memories` 创建 2 条 confirmed 记忆，`/api/memory-insights` 返回 memories_confirmed=2；
  - 连接器同步：POST `/api/data-sources`（kind=local_dir, root=项目外目录）成功，`/api/data-sources/{id}/sync` 返回 success（created=1），sync-logs 正常；
  - 提醒推进：启用 daily_review_enabled 后 tick 自动生成每日复盘提醒并推进 scheduled→due，手动创建提醒同样经 tick 至 due、`acknowledge` 至 acknowledged（domain 与 API 双路径均验证）；
  - 测试基线：修复 `tests/test_reminder_scheduler_recovery.py` 日期敏感缺陷（第二次 run_tick 前将租约置为已过期），全量 pytest 487 passed, 1 warning；compileall 通过。
- [x] **T042** **文档一致性核对**：`docs/`（architecture/module-responsibilities/database/config）与 plan.md Project Structure、data-model.md、contracts/api-contract.md 保持一致，无虚构接口。

---

**状态统计**：42 个任务全部完成（`[x]`），无待实现/待执行任务。
*（内容由AI生成，仅供参考）*
