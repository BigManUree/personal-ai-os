# 008 实现计划

## 组件与顺序

1. `app/schema_migrations.py`：`_migrate_chunks_fts(conn)` — 虚拟表 + 3 触发器 + 增量回填；`app/main.py` `init_db` 调用。
2. `app/retriever.py`：`_fts_boost(conn, query)` 返回 `{chunk_id: bonus}`；`search_chunks` 在 lexical 打分后叠加重排；任何 sqlite3.Error → 空 dict（静默回退）。
3. `app/api/chat.py`：`_chat_prepare(message, conversation_id)` 抽出公共前置（conversation 校验/创建、history、用户消息落库、profile）；`call_llm_stream()` 生成器；`POST /api/chat/stream` 用 `StreamingResponse(media_type="text/event-stream")`，内部按 SSE 帧组装。
4. `frontend/src/pages/Chat.vue`：`send()` 改 fetch 流式；保留原 UI 结构。
5. `tests/test_fts_and_stream.py`：契约测试。
6. `npm run build` + Playwright 冒烟 + 全量 pytest。

## 关键实现决策

- **trigram 最短 3 字符**：查询词 <3 字符的（如「跑」）FTS 不命中，由 lexical bigram 兜底——两套机制互补而非替换。
- **phrase 化**：`'"' + term.replace('"','') + '"'` 逐词拼接，FTS 关键字（AND/OR/NEAR）不会泄漏。
- **bm25 bonus**：`bonus = max(0.0, 3.0 + rank)`（rank 为负数，越小越相关）→ 命中越多分越高；lexical 分数保持整数语义不破坏既有断言（score 可能变 float，测试只断言 >0 与相对序）。
- **SSE 帧**：`event: <name>\ndata: <json>\n\n`；delta 的 json 是 `{"t": "..."}`，避免逐字符 json 开销。
- **流式期间不持有 DB 连接**（LLM 等待 60s 会锁写）：前置阶段用完即关，收尾阶段重开连接落库。

## 回滚

- `git revert` 单提交；文件级备份：`temp/bak-retriever-009.py`、`temp/bak-chat-009.py`、`temp/bak-chat-vue-009.vue`。
- FTS 表为纯派生数据，回滚后残留 `document_chunks_fts` 不影响任何查询（旧代码不读它）；`DROP TABLE IF EXISTS document_chunks_fts; DROP TRIGGER ...` 可彻底清除。
