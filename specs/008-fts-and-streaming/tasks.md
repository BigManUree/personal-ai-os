# 008 任务清单

- [x] T001 `_migrate_chunks_fts`：虚拟表 + 触发器 + 增量回填，接入 `init_db`
- [x] T002 `retriever._fts_boost` + `search_chunks` 重排，异常静默回退
- [x] T003 FTS 迁移幂等测试（两次 init_db、计数一致）
- [x] T004 FTS 触发器同步测试（新增/更新/删除 chunk）
- [x] T005 短查询（<3 字符）lexical 回退测试
- [x] T006 既有检索契约复跑（咖啡/RAG/legacy 迁移）
- [x] T007 `_chat_prepare` 抽取，`/api/chat` 行为不变
- [x] T008 `call_llm_stream`（本地分块 + OpenAI SSE 解析）
- [x] T009 `POST /api/chat/stream` SSE 端点（meta/delta/done/error 事件序）
- [x] T010 stream 落库 + extraction 触发测试
- [x] T011 stream 对话复用与 404 测试
- [x] T012 Chat.vue 流式渲染（fetch + ReadableStream、增量更新、错误显示）
- [x] T013 `npm run build` + Playwright 聊天流式冒烟
- [x] T014 全量 pytest + ruff + 真实库守卫
- [x] T015 文档同步（testing/progress/current-task/module-responsibilities/decisions）
