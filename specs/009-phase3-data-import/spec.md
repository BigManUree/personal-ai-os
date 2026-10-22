# 009 · Phase 3 数据接入 — 实现状态对照（已实现）

对照 `docs/PLAN.md` 阶段 3 的验收标准逐条核对。**结论：全部实现**，分布在 feature 001（MVP 文档）/ 003（工作项）/ 007（凭据）与既有连接器模块。

## 验收标准对照

| PLAN 验收标准 | 实现位置 | 状态 |
|---|---|---|
| PDF、Word、Markdown 导入 | `app/document_parse.py`（PDF/DOCX/TXT/MD）+ `POST /api/documents/upload`（类型/损坏校验 400） | ✅ |
| 数据可增量同步 | `POST /api/data-sources/{id}/sync`（按 `sync_enabled`/`sync_interval_minutes` 增量，`consecutive_failures` 退避）；调度由 `ConnectorScheduler` 驱动 | ✅ |
| 查看来源 | chunks 带 `document_id`/`page_hint`/`heading`；聊天回答引用 `【资料证据】[资料#id]《文件名》`（`docs/testing.md` 0.8/0.11 冒烟回显） | ✅ |
| 追踪失败记录 | `GET /api/data-sources/{id}/sync-logs`（`/api/data-sources/{id}/sync` 响应含 created/failed 计数） | ✅ |
| 撤销授权 | `POST /api/data-sources/{id}/revoke`、`DELETE /api/data-sources/{id}`（级联清 chunks + FTS 行）、`DELETE /api/data-sources/{id}/credential` | ✅ |
| 授权数据源（GitHub 等） | github/http_api/local_dir 连接器 + Fernet 加密凭据（write-only，feature 007） | ✅ |

## 附加能力（超出 PLAN 原文）

- FTS5 trigram 索引与 bm25 加成（feature 008）。
- 连接器调度生命周期测试（`tests/test_connector_schedule.py`）。

## 未做 / 豁免

- 日程类数据源（日历）未接——PLAN 原文为「日程、任务、GitHub 项目等」的「等」位，任务类已由用户手动维护（feature 003）。需要时按连接器接口新增一个 kind 即可。
