# 模块职责与依赖规则

更新时间：2026-10-22 13:25 CST

本文件只记录当前真实存在的模块。`app/` 采用按领域扁平组织；没有实际用途的 `api/`、`repositories/`、`utils/` 目录本轮不创建。

| 模块 | 主要职责 | 主要入口/输出 | 允许依赖 | 禁止事项/相关测试 |
|---|---|---|---|---|
| `app/main.py` | FastAPI 组装：lifespan、中间件、`init_db()`（schema DDL + 调迁移）、index/health/approvals 路由；`include_router` 挂载 `app/api/` 九个域 router；re-export 测试按值 import 的符号 | `app`、`init_db()`、HTTP JSON/静态文件 | 全部领域模块、`app/api/*`、FastAPI、sqlite3 | `DB_PATH`/`db()`/`record_behavior_event` 是全仓打点单一事实源；先校验提醒配置再初始化数据库；默认不启动提醒循环；健康检查只暴露脱敏状态 |
| `app/api/` | 按域拆分的 APIRouter（memory/reminders/documents/work/analytics/profile_items/connectors/recommendations/chat），路由处理函数与 Pydantic 模型 | Python | `app.main`（`db`/`now_iso` 等经 `_root` 晚绑定）、各领域模块 | 不自行读环境变量构造路径，一律经 `app.main.db()`；跨 router 共享助手放 documents/chat |
| `app/schema_migrations.py` | 幂等 SQLite 迁移（conn 参数化纯函数） | Python | 传入的连接 | 只做条件 ALTER/UPDATE，不删数据，可重复执行 |

| `model_config.py` | OpenAI 兼容模型环境配置 | `load_model_config()` | `os`、dataclass | 不发请求、不记录密钥；`tests/test_model_config.py` |
| `memory_extract.py` | 从最近对话构造提示、调用模型、校验候选 | `extract_candidates_from_messages()` | httpx、`model_config`、Pydantic | 不写库；网络在测试中 mock |
| `memory_match.py` | 记忆相似度、重复/冲突词法判断 | 匹配结果数据结构 | 标准库 | 不做人审决策 |
| `retriever.py` | 记忆、文档片段、项目/任务关键词检索 | `search_memories()`、`search_chunks()`、`search_work_items()` | sqlite3、`memory_match` | 只检索已授权/有效数据；未来可替换为混合检索 |
| `document_parse.py` | PDF/DOCX/MD/TXT 字节解析与文件校验 | `Page`、解析异常 | pypdf、python-docx | 不执行文件、不上网；`tests/test_documents.py` |
| `document_chunk.py` | 文本清洗、分块、页码/标题来源 | chunk 数据结构 | `document_parse.Page` | 不解析二进制 |
| `behavior_events.py` | 最小 append-only 行为事件与回填 | `record_event()`、`backfill_events()` | sqlite3 | 不存标题/正文/密钥；`tests/test_behavior_events.py` |
| `analytics.py` | 确定性统计、窗口、对比和连接器健康指标 | analytics 结果对象/字典 | sqlite3、标准库 | 不调用 LLM；`tests/test_analytics*.py` |
| `profile_propose.py` | 云端画像候选生成与校验 | `propose_profile_items()` | httpx、Pydantic、`model_config` | 只产生 pending 候选，不写库 |
| `recommendations.py` | 建议领域模型、存储、去重、冷却、状态机和 run 记录 | 存储函数/异常 | sqlite3、Pydantic | 不写记忆/画像；`tests/test_recommendations_store.py` |
| `recommendation_engine.py` | 证据收集、规则、可选模型润色、生成 run | 生成函数/模型调用接缝 | analytics、recommendations、httpx、`model_config` | 模型不能生成数字事实；默认不启用模型；`tests/test_recommendation_engine.py` |
| `recommendation_feedback.py` | 建议反馈/结果存储与状态约束 | feedback/outcome CRUD | sqlite3、recommendations | 不改变建议或个人事实；`tests/test_recommendation_feedback_store.py` |
| `recommendation_stats.py` | 建议有效性只读统计 | 统计字典 | recommendation_feedback、sqlite3 | 不写库 |
| `connectors.py` | connector 基类、注册中心、标准条目、配置密钥检查 | `BaseConnector`、registry、异常 | document_parse、Pydantic | 不直接访问数据库/FastAPI；`tests/test_connectors.py` |
| `github_connector.py` | 单仓库只读 GitHub connector | fetch 标准条目 | httpx、connectors | api.github.com 白名单、不跟随重定向；mock 网络测试 |
| `httpapi_connector.py` | 单 GET JSON API connector | fetch 标准条目 | httpx、connectors、SSRF 校验 | 默认 HTTPS、拦截私网/元数据地址；mock 网络测试 |
| `connector_scheduler.py` | 用户 opt-in 的数据源同步 asyncio 循环 | `ConnectorScheduler.start/stop/tick()` | asyncio | 进程内串行，不提供跨进程锁；`tests/test_connector_schedule.py` |
| `secret_crypto.py` | connector 凭据 Fernet 加解密和 key 解析 | `encrypt_secret()`、`decrypt_secret()` | cryptography、文件系统 | 不把密钥写入日志/普通配置；`tests/test_connectors.py` 等 |
| `reminders.py` | 提醒设置、CRUD、规则、安静时段、状态机 | 存储/规则函数 | sqlite3、Pydantic、analytics 类型 | 不含 HTTP/后台任务；`tests/test_reminders_store.py` 等 |
| `reminder_scheduler.py` | 确定性同步 tick、run recovery、边界 heartbeat/停止检查、fencing 写入 | `run_tick()`、guarded 函数 | reminders、lock、sqlite3、threading | 不启动 asyncio/FastAPI、不发通知；提醒 scheduler 系列测试 |
| `reminder_scheduler_service.py` | opt-in asyncio 生命周期、单线程同步执行器、状态快照、busy/error 分类、tick 耗时日志 | `ReminderSchedulerService.start/stop/tick_once()` | asyncio、sqlite3、scheduler、lock | 不拥有跨进程锁、不发通知、默认 disabled；`tests/test_reminder_scheduler_lifecycle.py` |
| `reminder_scheduler_lock.py` | SQLite 跨进程租约、TTL、heartbeat、fencing | acquire/heartbeat/release | sqlite3、uuid、socket | 多进程互斥权威；`tests/test_reminder_scheduler_lock.py`、fencing 测试 |
| `notification_delivery.py` | 通知事件/投递记录 schema、幂等键、claim、重试、死信、stale 恢复、fake channel | `create_reminder_due_outbox()`、`claim_next_delivery()`、`process_delivery_batch()` | sqlite3、reminders | 不发真实通知、不导入 FastAPI；`tests/test_notification_delivery.py` |
| `notification_delivery_service.py` | 默认关闭的 asyncio 控制循环和单线程 fake 投递 worker | `NotificationDeliveryService.start/stop/tick_once()`、`load_config()` | asyncio、sqlite3、notification_delivery | 只允许 `fake_test`；进程内锁不替代数据库领取；同测试文件 |
| `static/` | Vite 构建产物（Vue 3 UI） | 生成的 HTML/JS/CSS | `/api/*` | 由 `frontend/` 生成，不手改，不内嵌密钥，不绕过后端校验 |
| `frontend/src/` | Vue 3 前端源码 | `.vue` / `.ts` | `/api/*` | 不内嵌密钥，不绕过后端校验 |
| `frontend/src/components/` | 可复用 UI 原子：`Modal.vue`（弹层/遮罩/Esc）、`ProjectForm`/`GoalForm`/`TaskForm`（仅收集字段并 `emit('submit')`） | 组件 | Vue 3 | 不做 HTTP 请求、不持有列表状态；`tests/test_work_ui.py` 断言字段与枚举 |
| `frontend/src/pages/Work.vue` | 项目/目标/任务容器：全量加载、状态过滤、快捷动作、删除确认、项目详情 | 页面 | `api.ts`、`toast.ts`、上列组件 | 所有写请求必须经 `projectBody`/`goalBody`/`taskBody`（后端 PATCH 为全量替换）；`tests/test_work_ui.py`、`frontend/e2e_work.mjs` |
| `frontend/e2e_work.mjs` | 工作项 CRUD 的 Playwright 冒烟脚本（需手工起临时库实例） | Node ESM 脚本 | `playwright`、运行中的服务 | 只允许指向 127.0.0.1 临时库实例，不得指向真实开发库 |
| `frontend/src/components/OutcomeForm.vue` | 建议结果登记：GET 回填、PUT 部分更新、DELETE 删除、指标观察编辑（≤10 条） | 组件 | `api.ts`、`toast.ts` | 只对 `accepted` 建议渲染；`source`/`attribution` 固定值不下发控件；`tests/test_recommendation_insights_ui.py` |
| `frontend/src/components/RecommendationInsights.vue` | 只读统计（状态/反馈/采纳/登记/评级/指标对比/口径说明）+ 最近 10 次生成历史 | 组件 | `api.ts` | 不写库、不改统计口径；比率必须经 `fmtRatio()`（null → 「—」）；同测试文件 |
| `frontend/e2e_insights.mjs` | 建议结果登记与统计面板的 Playwright 冒烟脚本 | Node ESM 脚本 | `playwright`、运行中的服务 | 需配合 `scripts/e2e/seed_insights.py` 播种，只指向临时库 |
| `frontend/e2e_snooze.mjs` | 提醒延期与周期对比的 Playwright 冒烟脚本 | Node ESM 脚本 | `playwright`、运行中的服务 | 需配合 `scripts/e2e/seed_snooze.py` 播种，只指向临时库 |
| `frontend/e2e_credential.mjs` | 连接器凭据的 Playwright 冒烟脚本 | Node ESM 脚本 | `playwright`、运行中的服务 | 需配合 `scripts/e2e/seed_credential.py` 播种，只指向临时库 |
| `frontend/src/components/ProfileItemForm.vue` | 画像条目新建/编辑表单：手动类别 6 种（不含 `inference`）、来源 6 种（不含 `model`）、日期控件 | 组件 | Vue 3 | 不做 HTTP 请求；`tests/test_profile_items_ui.py` 断言枚举排除 |
| `frontend/src/pages/Persona.vue` | 画像条目容器：文本画像摘要、状态 tab（含显式「已拒绝」）、按 status 分发的动作矩阵、修订历史、「生成推测」 | 页面 | `api.ts`、`toast.ts`、`Modal`、上列表单 | 状态机权威在服务端；409 后刷新列表；`tests/test_profile_items_ui.py`、`frontend/e2e_profile.mjs` |
| `frontend/e2e_profile.mjs` | 画像条目管理的 Playwright 冒烟脚本 | Node ESM 脚本 | `playwright`、运行中的服务 | 需配合 `scripts/e2e/seed_profile.py` 播种，只指向临时库 |

## 依赖规则

- 允许：`main.py` -> 领域模块；领域模块 -> 更底层领域模块/标准库；跨领域共享逻辑必须有至少两个真实使用方。
- 禁止：`app/` 领域模块（非 api）导入 `app.main`；存储模块依赖 FastAPI；调度锁依赖 HTTP/LLM；通知领域模块加载真实渠道或导入 FastAPI；前端存放密钥。
- 例外（2026-10-22 拆分引入，见 `docs/decisions.md`）：`app/api/*` router 允许 `import app.main as _root` 晚绑定 `db()`/`now_iso`/`record_behavior_event` 等打点符号——这是 55 处测试 patch `app.main.DB_PATH` 兼容的代价；除此之外 router 不得触碰 main 其它状态。
- 循环依赖处理：若新调用形成环，优先抽取纯函数或数据结构，不通过延迟导入掩盖架构问题。

## 测试定位

- 纯函数/规则：直接调用领域模块，使用显式时间和替身。
- API：`TestClient` + 临时数据库 + monkeypatch `app.main.DB_PATH`。
- 外部网络：httpx `MockTransport`/DNS 打桩。
- SQLite 并发：真实多连接；服务多进程、lease、fencing 和 stale recovery 使用真实 OS 子进程和临时库。

| `.github/workflows/ci.yml` | 双平台 CI（ruff + compileall + pytest；前端 npm ci + build） | GitHub Actions | GitHub 托管 runner | 数据库与密钥永不入库，CI 不接触真实数据 |
| `scripts/e2e/*.py` | 各页面 Playwright 冒烟的播种脚本（从 temp/ 收编入库） | Python 脚本 | 临时 SQLite 文件 | 只写系统 Temp 下的隔离库，不得指向真实开发库 |
