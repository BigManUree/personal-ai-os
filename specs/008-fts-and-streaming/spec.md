# 008 · FTS5 文档检索 + 聊天流式输出

## 背景

审查报告 E 项遗留的最后两个功能缺口：`retriever.search_chunks` 是纯 Python lexical 扫描（每次取 100 行逐个打分），聊天回答一次性返回（长回答等待感强）。

## 范围

### FR-1 FTS5 索引（document_chunks）

1. `document_chunks_fts`：external-content FTS5 虚拟表（`content='document_chunks'`，`content_rowid='id'`，列 `content`/`heading`），tokenize 用 `trigram`（unicode61 对中文整串成 token，不可用；trigram 支持中英文子串，SQLite ≥3.34）。
2. 触发器同步 INSERT/UPDATE/DELETE；`init_db` 幂等回填存量行（`WHERE id NOT IN (SELECT rowid FROM fts)`）。
3. 删除文档级联删 chunk 后，FTS 中同步消失（AD 触发器）。

### FR-2 search_chunks 使用 FTS 提分

1. 现有 lexical 打分逻辑保持不变（契约：`咖啡偏好 美式` bigram 命中、score>0、结构不变）。
2. 查询含 ≥3 字符词时追加 FTS MATCH（phrase 化防注入，bm25 排序取前 50），命中 chunk 加成后重排；FTS 异常（旧 SQLite 无 trigram 等）静默回退纯 lexical。
3. 返回结构（chunk_id/document_id/filename/page_hint/heading/excerpt/score）与每文档最多 2 条的折叠规则不变。

### FR-3 `/api/chat/stream`（SSE）

1. 保留 `POST /api/chat`（兼容 + citations 测试）。
2. 新端点事件序：`meta`（conversation_id、used_* 摘要）→ 多个 `delta`（文本片段）→ `done`（完整 answer + used_* 全量）；LLM 异常发 `error` 事件（HTTP 200 内），不中断连接。
3. 本地演示模式按 ~40 字符分块 yield；LLM 模式用 httpx 流式（OpenAI `data:` 行解析 `choices[0].delta.content`），流式不重试。
4. 流结束后完整 answer 落库 `messages`，并照旧触发 memory extraction。
5. 消息历史读取、用户消息落库、conversation 创建逻辑与 `/api/chat` 一致。

### FR-4 前端 Chat.vue 流式渲染

1. `send()` 改 fetch + ReadableStream 解析 SSE；assistant 消息随 delta 增量更新，滚动跟随。
2. `error` 事件或网络失败时在消息区显示错误；`done` 后刷新会话列表、更新 conversation_id。

## 非目标

- 不做 memories/projects/tasks 的 FTS（lexical 已够用，数据量小）。
- 不引入向量检索。
- 不改 `/api/chat` 响应结构。
- 不做多模型流式协议适配（只 OpenAI 兼容 `chat/completions`）。

## 验收

- pytest：既有 569 用例零回归；新增 FTS 迁移/同步/回退与 stream 事件序、落库、错误路径契约测试。
- Playwright：聊天页发送消息可见增量渲染，15 项既有冒烟不回归。
