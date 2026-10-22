# 阶段 3：个人数据中心与统一知识检索

> 状态：**第七轮 3.5.3（第二批）定时调度 — 已完成**（2026-10-12）：data_sources 增 4 列（默认全部关闭），`app/connector_scheduler.py` 单后台串行轮询 + 指数退避，新增 `/schedule` 接口与前端开关，新增 9 个测试，全量 **130 passed, 1 skipped**，API/UI 冒烟通过，真实库无残留。
> 上一状态：**第六轮 3.5.3（第一批）通用 HTTP API 只读连接器 — 已完成**（2026-10-11）：HttpApiConnector（kind=`http_api`，单 GET JSON 列表 + 字段映射 + SSRF/DNS 防护 + Bearer 凭据）注册上线，新增 10 个测试，全量 **121 passed, 1 skipped**，Playwright UI 冒烟通过，真实库无残留。
> 上一状态：**第五轮 3.5.2（第二批）GitHub 只读连接器 — 已完成**（2026-10-11）：凭据 Fernet 加密存放（connector_credentials 新表，密钥独立文件）+ GitHubConnector 注册上线，新增 14 个测试，全量 **111 passed, 1 skipped**，Playwright UI 冒烟通过，真实库无残留。
> 上一状态：**第四轮 3.5.2（第一批）本地目录只读连接器 — 已完成**（2026-10-11）：LocalDirectoryConnector 注册上线，新增 8 个测试（1 个符号链接用例在无权限环境跳过），全量 **97 passed, 1 skipped**，Playwright 冒烟通过，真实库无残留。
> 上一状态：**第三轮 3.5.1 连接器框架 — 已完成**（2026-10-11）：统一接口/注册中心/数据源管理/同步日志 + 内置 mock 连接器，新增 15 测试，全量 **89 passed**，Playwright 冒烟通过，真实库迁移无损。本轮不接真实 GitHub、不自动扫描目录；下一轮为 3.5.2（本地目录只读连接器优先，GitHub 只读其次）。
> 前置条件：阶段 2 长期记忆引擎迭代 01–05 已全部完成（34 测试通过，含审核/去重/冲突/引用/注入隔离）。
> 第一轮范围（依据开发提示词第 9 节）：**3.1 文件上传/解析/管理 + 3.2 分块与来源追踪 + 3.3 关键词片段检索**；本轮不做项目/目标/任务（3.4）与连接器框架（3.5），不接任何第三方平台。

## 一、阶段目标（5 个迭代全貌）

| 迭代 | 交付 | 验收 |
| --- | --- | --- |
| 3.1 | PDF/DOCX/MD/TXT 上传、内容校验、解析、文件管理 | 主要格式可解析；可查看状态、删除；非法/损坏/重复被拒 |
| 3.2 | 文本清洗、分块、来源（文件/页码/标题）追踪 | 每个片段能定位到原始文档 |
| 3.3 | 统一检索与回答引用（记忆 + 文档片段） | 回答引用具体文件片段；证据不足明说；删除后片段不可检索 |
| 3.4 | 项目、目标、任务模块（projects/project_events/goals/tasks） | 可管理项目状态与任务进度（后续轮次） |
| 3.5 | data_sources/sync_logs 连接器框架 | 同步状态、失败重试、授权撤销（后续轮次） |

核心原则：**不把所有数据转成长期记忆**。手册/资料→文档片段；反复验证的个人技术偏好→长期记忆；项目与截止日期→项目/任务表。

## 二、第一轮（本轮）数据模型

### 2.1 `documents` 扩展（增量 ALTER，保留数据）
新增：`file_hash TEXT`（sha256，去重）、`source_type TEXT DEFAULT 'manual_text'`（manual_text/upload）、`status TEXT DEFAULT 'parsed'`（parsed/failed）、`original_filename TEXT`、`file_ext TEXT`、`byte_size INTEGER`、`error TEXT`、`chunk_count INTEGER DEFAULT 0`、`updated_at TEXT`。旧行保持可检索。

### 2.2 新增 `document_chunks`
| 字段 | 用途 |
| --- | --- |
| id | 主键 |
| document_id | 外键 → documents(id) ON DELETE CASCADE |
| chunk_index | 片段序号（从 0） |
| content | 清洗后的片段文本 |
| page_hint | PDF 页码（其他格式为段号，可空） |
| heading | 该片段所属标题（MD 取最近标题；可空） |
| char_count | 片段字符数 |
| created_at | 时间 |

删除文档时在同一事务删除其全部分块（级联 + 显式删除双保险）。向量索引本阶段不存在，无需额外清理。

## 三、模块拆分（独立文件，不堆 main.py）

- `app/document_parse.py`：字节 → 纯文本。
  - 先做**内容嗅探**（magic bytes：`%PDF`、DOCX zip+`[Content_Types].xml`、UTF-8/UTF-16 文本），不只信扩展名。
  - PDF：`pypdf`；DOCX：`python-docx`（段落 + 表格）；TXT/MD：编码探测（utf-8 优先，gbk 兜底）。
  - 限制：单文件 ≤ 10 MB；允许扩展名白名单 .pdf/.docx/.md/.markdown/.txt；解压/解析后纯文本 ≤ 200,000 字符；超限/损坏/类型不符抛 `DocumentParseError`。
- `app/document_chunk.py`：清洗（去零宽字符、统一空白、去过多空行）+ 分块（优先段落边界，目标约 900 字、最大 1200 字、相邻重叠 100 字）；MD 跟踪最近 `#` 标题；PDF 接收页码映射（解析器逐页产出）。
- `app/retriever.py` 新增 `search_chunks(conn, query, limit)`：对 chunks 做关键词评分（bigram/词），同文档多片段聚合，返回 chunk_id/document_id/filename/page_hint/heading/score/excerpt；接口仍为可替换检索器（为 pgvector/混合检索预留）。

## 四、API（兼容现有接口）

- 新增 `POST /api/documents/upload`（multipart `file`）：校验 → hash 去重（409）→ 解析 → 分块 → 事务写 documents/document_chunks，返回 `{id, filename, status, chunk_count}`；失败 4xx 不落库任何残留。
- `GET /api/documents`：增补 `status/chunk_count/byte_size/source_type`。
- `GET /api/documents/{id}`：详情 + 分块列表（内容截断），供来源预览。
- `DELETE /api/documents/{id}`：事务删文档+分块。
- `POST /api/documents/text`（保留）：同样走「解析后分块」路径（视为 manual_text 来源），保持旧行为可用。
- `/api/chat`：`used_documents` 改为**片段级引用**（document_id/chunk_id/filename/page_hint/heading/score）；系统 prompt 的 document_data 只发送命中片段（不发整篇），继续按不可信数据包裹。

## 五、安全与隐私（第一版即内建）

- 仅处理主动上传；不扫描磁盘；不读其他账号数据。
- 白名单扩展名 + magic bytes 双重校验；安全文件名（落库名用 sha256 前缀 + 清洗后的原名；本版本地不落原始文件，只存解析文本）。
- 不执行上传文件中的脚本；文档文字一律为不可信数据，防提示词注入。
- 默认不向云端发送整篇文档，只发送命中片段（每片截断）。
- 删除文档即删除其全部分块；重复上传按 sha256 拒绝。

## 六、本轮测试（pytest，全部离线 mock，不产生 API 费用）

1. TXT/MD/PDF/DOCX 正常解析并分块（测试内生成最小合法 PDF/DOCX）。
2. 非法扩展名、扩展名与内容不符（文本伪装 .pdf）、损坏 PDF/DOCX、超大文件、超长文本 → 4xx/抛错且不落库。
3. sha256 重复上传 → 409。
4. 分块可定位来源（document_id/chunk_index/page_hint/heading）。
5. `search_chunks` 相关性排序；聊天 used_documents 为片段级且只含命中片段。
6. 删除文档后其分块不再可检索。
7. 旧 documents 表迁移后旧数据仍在且可检索（兼容回填）。
8. 模型不可用时上传/删除/列表照常工作。
9. 文档内恶意指令在 prompt 中仍被标记为不可信数据。

## 七、非目标（本轮明确不做）

- 项目/目标/任务 CRUD（3.4）、连接器框架与外部平台（3.5）。
- 向量数据库 / embedding / pgvector（仅保留检索器替换接口）。
- 原始文件持久化到磁盘、OCR、扫描件识别、PDF 图片提取。
- 多用户/鉴权（仍限本机 127.0.0.1）。

---

# 第二轮实施细则（2026-10-11，迭代 3.4：项目 / 目标 / 任务）

> 前置校验：阶段 2 迭代 01–05 已闭环（34 测试），阶段 3 第一轮 3.1–3.3 已完成（51 测试全绿）。满足「长期记忆引擎未完成不叠加新功能」的前提。

## A. 数据模型（全新增表，CREATE IF NOT EXISTS，不动旧表数据）

- `projects`：id、name（必填）、description、tech_stack、repo_url、status（planning/active/paused/completed/archived，默认 active）、created_at、updated_at。
- `project_events`：id、project_id（外键 **ON DELETE CASCADE**）、event_type（progress/decision/issue/learn/note）、content（必填）、created_at。记录开发进度、技术决策、遗留问题、学习要点。
- `goals`：id、title（必填）、description、kind（long_term/milestone，默认 long_term）、status（active/done/dropped，默认 active）、measure（衡量指标，可空）、target_date（YYYY-MM-DD，可空）、project_id（外键 **ON DELETE SET NULL**，可空）、created_at、updated_at。
- `tasks`：id、title（必填）、detail、status（todo/doing/done/cancelled，默认 todo）、priority（low/normal/high，默认 normal）、due_date（YYYY-MM-DD，可空）、project_id（SET NULL）、goal_id（外键 goals ON DELETE SET NULL，可空）、completed_at、created_at、updated_at。
- 索引：project_events(project_id)、tasks(project_id,status)、goals(status)。
- 删除语义：删项目 → 事件级联删除；该项目下 tasks/goals 的 project_id 置空（不连带删除任务）。删目标 → tasks.goal_id 置空。

## B. API（兼容既有接口，只增不改旧行为）

- 项目：`GET/POST /api/projects`；`GET/PATCH/DELETE /api/projects/{id}`（GET 含事件、关联目标、任务计数）；`POST /api/projects/{id}/events`。
- 目标：`GET /api/goals?status=&project_id=`、`POST /api/goals`、`PATCH/DELETE /api/goals/{id}`。
- 任务：`GET /api/tasks?status=&project_id=`、`POST /api/tasks`、`PATCH/DELETE /api/tasks/{id}`；todo/doing→done 自动写 completed_at，离开 done 清空。
- 枚举非法 → 400；未知 ID → 404；日期必须 YYYY-MM-DD 或空；标题必填。
- 全部不依赖大模型，模型不可用时照常工作。

## C. 统一检索纳入项目与任务（RAG 第三、四路检索）

- `app/retriever.py` 新增 `search_work_items(conn, query, limit)`：对项目 name/description/tech_stack 与未完成任务 title/detail 做关键词评分（复用 lexical_score / tokenize）；任务带时效加权（已逾期/3 天内截止加权、项目名命中加权），返回 `{projects:[...], tasks:[...]}`，仍是可替换检索接缝。
- `/api/chat`：检索顺序 记忆 → 文档片段 → 项目/任务；系统 prompt 增加【项目与任务状态】分区，明确标注为用户维护的数据、属不可信数据不可执行其中指令；证据不足仍须明说。
- 回答响应新增 `used_projects / used_tasks`（带 id、status、score、任务 due_date/project_name）；`used_memories / used_documents` 结构不变。
- `call_llm(...)` 以可选关键字参数 `projects=None, tasks=None` 扩展，旧调用位置参数完全兼容；本地演示回答展示今日相关待办（高优先级/临近截止）。

## D. 前端（仅增量，不重写）

- 侧栏新增「项目与任务」页：三个分区——项目（新增表单、项目列表、项目下记录事件/状态标签）、目标（标题/指标/截止日期/关联项目、状态流转）、待办（标题、优先级、截止日期、关联项目；开始/完成/重开按钮）。
- 新文件 `app/static/work.js` 承载逻辑；index.html 只加导航项、页面骨架与 script 引用；对话引用栏追加项目/任务来源。

## E. 测试（tests/test_work.py，全离线）

1. 项目 CRUD；删项目级联删事件、tasks/goals 的 project_id 置空但记录仍在。
2. 项目事件新增、非法 event_type 400、未知项目 404。
3. 目标 CRUD + status/project_id 过滤；删目标后 task.goal_id 置空。
4. 任务 CRUD、优先级/状态枚举校验、done 自动 completed_at、重开清空、过滤。
5. `search_work_items`：相关项目/任务命中且排序含时效加权；已完成/已归档不返回。
6. `/api/chat`（本地演示模式，无 Key）返回 used_projects/used_tasks 且只含命中项；无 LLM 时项目/任务接口正常。
7. `init_db()` 重复执行幂等；旧表数据不受影响（新表独立）。
8. 任务/项目中的注入文字只作为数据进入 prompt（断言系统提示包含不可信数据声明，且不产生任何工具调用——本系统本无工具调用路径）。

## F. 非目标（本轮不做）

- 3.5 连接器框架（data_sources/sync_logs）、外部平台（Git 等）接入。
- 甘特图/看板拖拽/提醒推送/子任务/多用户。
- 向量检索、项目数据自动抽取入库（结构化数据仍由用户显式维护；AI 侧只做读取与建议）。

---

# 第三轮实施细则：3.5.1 数据源连接器框架（2026-10-11）

> 依据用户提供的《3.5 数据源连接器框架》提示词，本轮范围严格限定为 3.5.1：**框架 + mock 连接器测试**。3.5.2（本地目录/GitHub 只读）、3.5.3（通用 HTTP API、调度、授权管理增强）后续再做。

## G. 现状核对（实际代码，不假设）

- 文档入库已有单事务函数 `_store_parsed_document(conn, display_name, content_for_legacy, pages, ext, source_type, file_hash, byte_size)`（写 documents + document_chunks，返回 id/chunk_count）；上传路径用 sha256 做应用层去重（documents 表无 file_hash 唯一约束）。**连接器同步必须复用它**，不重写解析/分块。
- 删除文档路由已显式删 chunks；连接器侧的删除只能通过 source_items 定位“本数据源拥有的 document_id”，避免误删其他来源。
- 无 data_sources/sync_logs/source_items/connector_configs/sync_checkpoints 任何一张表，全部为新增。

## H. 模块设计

新增 `app/connectors.py`（不 import main，避免循环依赖；不访问网络、不碰数据库、不调用模型）：

- `StandardItem`（pydantic，字段全部校验）：`source_type`、`source_id`、`external_id`（同一数据源内稳定唯一）、`title`、`content`（1–200,000 字符，与现有解析上限一致）、`content_hash`（可空，缺省由内容 sha256 计算）、`source_url`（可空）、`updated_at`（可空）、`metadata`（白名单 dict，键名/字符串值做密钥特征拒绝；不允许出现 token）。
- `SyncResult`（dataclass）：`items: list[StandardItem]`、`next_cursor: str|None`、`deleted_external_ids: list[str]`。
- `BaseConnector(ABC)`（async）：`validate_config() -> None`（配置/授权不可用抛 ConnectorConfigError）、`fetch_changes(cursor=None) -> SyncResult`（网络/权限失败抛 ConnectorFetchError；**区分** `ConnectorDeleted`/权限错误与临时错误，用异常子类）、`close()`。
- `ConnectorRegistry`：`register(kind, cls)`（重复注册抛 ValueError）、`is_registered`、`available()`、`create(kind, config)`（未知类型抛 ConnectorUnknownError 并带清晰中文信息）。**注册是开发者在代码里做的事，没有任何 HTTP 接口能注册新类型**；注册 ≠ 授权某个用户数据源，二者分离。
- 内置 `MockConnector`（kind=`mock`）：从 config 静态读取 `items / next_cursor / deleted_ids / fail`，供框架验证与前端试用；仅 mock 允许“配置里直接带内容”，真实连接器不许这样。
- 安全工具：`redact_secrets(text)`（脱敏 sk-…/Bearer …/(token|key|secret|password)=…/AKIA…），供同步日志落库前调用；`assert_config_has_no_secrets(config)`。

异常层次：`ConnectorError` → `ConnectorConfigError` / `ConnectorUnknownError` / `ConnectorFetchError`（临时）/ `ConnectorAuthError`（撤销授权）/ `ConnectorUnavailable`（暂时不可访问）。临时错误与权限/删除错误分开，决定“不删数据”与“标记删除/不可访问”的不同处理。

## I. 数据库（纯新增 3 表，CREATE IF NOT EXISTS，旧数据零改动）

单用户本地应用，不拆 connector_configs/sync_checkpoints：非敏感配置直接放 data_sources.config_json，检查点放 data_sources.cursor（理由：避免为不存在的多用户/多配置过度设计）。

- `data_sources`：id、kind（连接器类型）、name、status（`active/paused/revoked`，默认 active；本轮无 unauthorized 中间态）、scope TEXT（用户授权范围的可读说明）、config_json TEXT（非敏感配置，写入前过密钥检查）、cursor TEXT（增量检查点）、last_synced_at、last_error（脱敏后）、created_at、updated_at。
- `sync_logs`：id、data_source_id（FK ON DELETE CASCADE）、started_at、finished_at、status（`running/success/failed`）、cursor_before、cursor_after、total/created/updated/unchanged/skipped/deleted/failed（INTEGER 计数）、error TEXT（脱敏摘要）。索引 (data_source_id, id)。
- `source_items`：id、data_source_id（FK ON DELETE CASCADE）、external_id、document_id（可空，关联 documents.id；documents 被删时不断言外键，应用层置空）、content_hash、title、status（`active/deleted`）、last_seen_at、created_at、updated_at；**UNIQUE(data_source_id, external_id)**；索引 (data_source_id, status)。

## J. 同步服务与规则（路由层调用，复用现有文档管线）

统一入口 `POST /api/data-sources/{id}/sync`：
1. 数据源须 `active`；paused/revoked → 409。先写一条 `running` 日志（cursor_before=当前 cursor）。
2. 在事务**外** `await connector.fetch_changes(cursor)`：
   - 抛 `ConnectorFetchError/ConnectorUnavailable`（临时）→ 日志置 failed + 脱敏 error，**cursor 不前进、不删任何文档**，数据源状态保持 active，返回 200 带 failed 摘要（不把一次网络错误抛成数据变更）。
   - 抛 `ConnectorAuthError` → 数据源置 revoked，日志 failed，同样不删数据。
3. 取数成功后开**单个事务**：
   - 逐项：StandardItem 非法 → skipped/failed 计数并继续；按 (data_source_id, external_id) 查 source_item：
     - 不存在：`_store_parsed_document`（source_type=`connector`，file_hash=content_hash，ext 取 metadata.ext 或 .txt）新建文档+分块 → 建 active source_item；created++。
     - 已存在且 hash 相同：unchanged++，刷新 last_seen_at。
     - 已存在且 hash 不同：删旧文档+分块、用同一 external_id 重建文档、更新 source_item.document_id/hash；updated++。
   - `deleted_external_ids`：仅对本数据源这些 source_item，删其 documents+chunks，source_item 置 status=deleted/document_id=NULL；deleted++。
4. 成功才把 next_cursor 写入 data_sources.cursor 与 sync_logs.cursor_after，last_synced_at 更新、last_error 清空，日志置 success 并写各计数。
- 跨数据源**删除隔离**：连接器文档即便内容 hash 与他源/上传文档相同也各自独立（不依赖 documents.file_hash 全局去重），删除只按本源 source_item 定位。
- 全程不调用大模型、不写 memories/profile_items。

## K. API（新增，不改旧接口行为）

- `GET /api/connector-types`：注册中心已注册类型（kind/label），只读。
- `GET/POST /api/data-sources`（POST 校验 kind 已注册、name 非空、config 无密钥）。
- `GET /api/data-sources/{id}`（含状态/计数/最近日志）；`PATCH`（改 name/scope/config；config 同样过密钥检查；revoked 不可改配置）。
- `POST /api/data-sources/{id}/sync`、`/pause`、`/resume`、`/revoke`；`DELETE /api/data-sources/{id}`（先按 source_items 删本源 documents+chunks，再删 source_items，sync_logs/行级联）。
- `GET /api/data-sources/{id}/sync-logs`。
- 未知 id 404、非法 kind/config 400、状态非法转移 409。

## L. 安全约束（本轮内建）

- 用户主动添加才存在数据源；无任何自动扫描/定时任务/后台调度；仅 127.0.0.1。
- 本轮无凭据连接器；config 出现密钥键名或 sk-/Bearer/AKIA 等特征一律 400；日志落库前 redact；错误信息不回显完整配置。
- 连接器代码不执行 shell、不读 config 授权范围之外的路径（路径/URL/SSRF 校验是 3.5.2/3.5.3 具体连接器职责，本轮接口已把目标限制设计为 validate_config 的强制环节）。
- 外部内容经现有文档管线成为不可信数据；检索 prompt 的不可信声明已存在，不为连接器开口子。
- 撤销授权 = revoked，同步立即被拒；删除数据源与撤销授权分开（撤销不自动删数据，删除才清理）。

## M. 前端（精简“数据源管理”入口，新文件 connectors.js）

侧栏新增「数据源」页：已注册类型展示、添加数据源（名称/类型/授权范围；mock 可直接填静态 items 试用）、列表（状态/最后同步/错误）、立即同步、暂停/恢复/撤销、删除、最近一次同步日志与计数。沿用现有样式，不改写旧页面。

## N. 测试（tests/test_connectors.py，全离线）

注册/重复注册/未知类型；StandardItem 校验（缺字段/超长/敏感 metadata/空 content）；数据源 CRUD + 未知 kind/config 密钥 400；首次同步建文档+分块+source_item 并存游标；增量（unchanged/updated 后旧内容不可检索/删除只影响本源）；重复同步幂等；临时异常不误删且游标不前（failed 日志）；授权撤销/暂停拒绝同步、resume 恢复；删除数据源只清理本源、他源同内容仍在；日志错误脱敏；init_db 两次幂等且旧数据不变；API happy path。

## O. 非目标（本轮不做）

真实本地目录/GitHub/HTTP API/MCP 连接器；后台调度/频率/Webhook；凭据加密库与 OAuth；连接器内容的二进制解析（StandardItem 本轮承载文本，二进制在 3.5.2 随本地目录连接器接入 parse_document）；把连接器内容接入记忆/画像自动提炼。

## P. 3.5.1 落地回填（2026-10-11）

- 新增：`app/connectors.py`（StandardItem/SyncResult/BaseConnector/ConnectorRegistry/MockConnector/异常体系/脱敏）、`app/static/connectors.js`、`tests/test_connectors.py`（15 项）。
- 修改：`app/main.py`（3 张表 DDL、数据源与同步共 8 条路由、复用 `_store_parsed_document`）、`app/static/index.html`（「数据源」页）、`README.md`。
- 数据库：纯新增 `data_sources / sync_logs / source_items`，无对旧表改动；真实库迁移后旧数据计数不变。
- 与计划的小偏差：同步接口对临时错误按提示词要求"不抛成数据变更"，返回 `200 + status=failed`（而非 HTTP 5xx），前端据此提示；数据源状态保持 active、cursor 不前、不删数据。
- 错误留档：`docs/errors/2026-10-11-phase35-connector-config-error-500.md`（域异常→HTTP 边界转换）。

## Q. 下一轮 3.5.2 实施计划（本地目录只读连接器优先）

1. 新增 `LocalDirectoryConnector`（kind=`local_dir`），严格只在用户显式授权的单一目录内：
   - `validate_config`：路径必须是绝对路径、必须位于用户白名单根目录之下、拒绝盘符根目录/系统目录/当前项目库目录、规范化并防 `..` 穿越；只接受 `.md/.markdown/.txt/.pdf/.docx`。
   - `fetch_changes`：递归（限制深度与文件数）枚举，mtime + 游标（记录每文件 mtime/hash 的检查点 JSON）做增量；二进制走现有 `document_parse.parse_document`，文本直接构造 StandardItem；删除通过"本轮未见的 external_id（相对路径）"得出。
   - 全程只读、不执行任何文件、不跟随符号链接逃逸授权目录。
2. 之后再做 `GitHubConnector`（kind=`github`）：仓库级只读细粒度 token（走专用凭据存放，不进 config_json）、超时/响应体上限/SSRF 仅允许 api.github.com 与原始内容域名、读 README/docs/提交记录；首次只做手动同步。
3. 暂不做：定时调度/Webhook/通用 HTTP API/MCP（留 3.5.3），不自动写记忆或画像。
4. 复用本轮同步事务、删除隔离、sync_logs、注册中心；新增连接器只需实现 BaseConnector 并注册。

---

# 第四轮实施细则：3.5.2（第一批）本地目录只读连接器（2026-10-11）

> 范围：仅实现 `LocalDirectoryConnector`（kind=`local_dir`）并注册；GitHub 只读连接器需要凭据安全存放，放到 3.5.2 第二批，本轮不做。仍无定时调度、无自动扫描（必须用户显式添加并手动点同步）。

## R. 设计

新增于 `app/connectors.py`（不 import main、不联网、不调模型）：

- config：`{"root": "<绝对路径>", "recursive": true, "max_depth": 6}`。扩展名固定白名单复用 `document_parse.ALLOWED_EXTENSIONS`（.md/.markdown/.txt/.pdf/.docx），不允许调用方放开。
- `validate_config`（创建数据源时与每次同步前各执行一次）：
  - root 非空、必须是**绝对路径**；`Path.expanduser().resolve(strict=False)` 规范化，防 `..` 穿越。
  - 必须存在且为目录；拒绝盘符根目录（如 `C:\`）、系统/敏感目录（%SystemRoot%、Program Files、用户主目录及其父、AppData）、**本项目目录与其 data 目录**（避免索引自身代码/数据库）。
  - recursive 必须 bool、max_depth 1–12。
- `fetch_changes(cursor)`（只读）：
  - 用 `os.walk(followlinks=False)` 枚举；超过 max_depth 不下钻；文件数硬上限 `MAX_LOCAL_FILES=200`；任何符号链接（`os.path.islink`）或 resolve 后逃逸出授权根的条目一律跳过。
  - cursor 为 JSON：`{相对路径(posix): {"m": st_mtime_ns, "s": size, "h": 内容hash}}`。
  - 新增/疑似变更（mtime_ns 或 size 不同）才读文件：字节统一交 `document_parse.parse_document`（复用 magic/大小/空文本校验），多页拼接为正文，构造 StandardItem（external_id=相对路径、title=相对路径、metadata={ext,path,size}，content_hash 取正文 sha256，与同步服务口径一致）；解析失败/空文本的单文件跳过并记日志，不中断整批。
  - mtime+size 未变 = 不产出 item（同步服务侧自然记 unchanged，不重新解析 PDF）；cursor 中存在但本轮磁盘已无的相对路径 → `deleted_external_ids`。
  - next_cursor 为本轮最新索引 JSON。删除判定完全由"本轮枚举结果"驱动，一次临时读取异常不会产生删除（枚举异常按临时错误抛 ConnectorFetchError，沿 3.5.1 既有路径：不删数据、游标不前）。
- 删除联动：相对路径即 external_id，源端删文件 → 同步事务删该 source_item 对应文档/分块（3.5.1 已实现，且只影响本源）。

## S. 安全边界

- 只读：不创建/修改/删除授权目录内任何文件，不执行脚本，不跟随符号链接逃逸；不扫描授权目录之外。
- 不主动扫描：无后台任务，用户在「数据源」页显式添加 `local_dir` 并手动同步。
- 解析后的正文按既有文档管线落为不可信数据；config 仍过密钥特征检查（虽然本地路径一般无密钥）。
- 枚举/读取阶段的非预期异常映射为 ConnectorFetchError（临时，保留数据），不映射成删除。

## T. 测试（tests/test_local_connector.py，全离线，用 tmp_path 造目录）

1. validate_config：相对路径/不存在/文件而非目录/盘符根/项目根 被拒；合法临时目录通过；非法 recursive/max_depth 被拒。
2. 首次同步：md/txt（含子目录、忽略不支持扩展名）产出 StandardItem，external_id 为 posix 相对路径、ext/title 正确、有 hash、next_cursor 含文件。
3. 增量：未改文件不再产出；改一个文件（os.utime 推进 mtime + 改内容）仅该文件产出；删一个文件进入 deleted_external_ids。
4. 越界符号链接被跳过（无法创建符号链接时 skip 该用例）。
5. 空文件/无法解析文件被跳过且不影响其他文件；超 max_depth/文件数上限受控。
6. 端到端 API：创建 local_dir 数据源 → 同步 → 知识库出现 connector 文档且可检索；第二次同步 unchanged；源端删文件再同步后对应文档/分块被清理、他源不受影响。
7. 注册中心内置类型变为 mock + local_dir（更新 3.5.1 相关断言）；init_db 幂等与既有功能回归由全量测试覆盖。

## U. 非目标（本轮不做）

GitHub/HTTP API/MCP 连接器；凭据加密存储；定时/Watchdog 自动同步；mtime+size 之外的内容级变更探测（如需要后续可加"仍抽样 hash"）；对扫描件 OCR。

## V. 3.5.2 第一批落地回填（2026-10-11）

- 修改：`app/connectors.py`（新增 `LocalDirectoryConnector` 并注册 `local_dir`；`SyncResult` 增 `unchanged_external_ids`；新增本地路径常量与系统目录/项目目录拦截、temp 目录豁免）、`app/main.py`（同步服务消费 unchanged id 并统一盖章 source provenance）、`app/static/connectors.js`（local_dir 配置提示）、`tests/test_connectors.py`（注册类型断言更新）、`README.md`。
- 新增测试：`tests/test_local_connector.py`（配置校验/首次同步/增量更新+删除/非递归与深度/坏文件跳过/符号链接逃逸/端到端 API/密钥拒绝）。
- 关键实现点：mtime_ns+size 增量、未变文件不重新解析 PDF、删除完全由“本轮枚举结果”驱动、单文件失败不阻断整批、相对路径(posix)即 external_id、只复用 `document_parse` 白名单与校验、`os.walk(followlinks=False)` 且 resolve 后二次确认未逃逸授权根。
- 深度语义最终确定：根目录=0，`max_depth=N` 允许 N 层子目录（0–12，默认 6）；是否递归由 `recursive` 单独控制（见 errors 留档）。
- 错误留档：`docs/errors/2026-10-11-phase352-local-connector-max-depth.md`。
- 下一批（GitHub 只读连接器）前置：先实现凭据安全存放（不进 config_json），再做 api.github.com/raw 域名白名单、超时与响应体上限、仓库级只读 token、提交记录标准化；仍为手动同步，不做调度。

# 第五轮实施细则：3.5.2（第二批）GitHub 只读连接器（2026-10-11）

> 范围：① 凭据安全存放前置（密文落库、密钥独立文件、绝不进 config_json/日志）；② `GitHubConnector`（kind=`github`，仓库级只读 fine-grained PAT），仅手动同步；③ 前端数据源页支持设置/更新令牌。仍不做定时调度、Webhook、通用 HTTP API、MCP（留 3.5.3），不自动写记忆/画像。

## W. 凭据安全存放（前置）

- 新增 `app/secret_crypto.py`：Fernet（cryptography，成熟必要依赖，加入 requirements）对称加密；密钥文件 `data/secret.key`（可用环境变量 `PAOS_SECRET_KEY_PATH` 覆盖），首次使用自动生成、`chmod 600`；密钥与数据库分离存放（单独拷走 DB 无法解密）。
- 新表 `connector_credentials(id, data_source_id UNIQUE FK ON DELETE CASCADE, ciphertext, created_at, updated_at)`：只存密文；任何 GET 接口只回 `has_credential: true/false`，永不回传明文。
- API（沿用现有命名风格）：
  - `POST /api/data-sources/{id}/credential` body `{secret}`：设置/更新令牌；
  - `DELETE /api/data-sources/{id}/credential`：清除令牌；
  - 数据源列表/详情附带 `has_credential`。
  - 创建数据源允许一次性携带 `secret` 字段（不进 config_json），落密文后再 validate_config。
- 同步时 `_build_connector` 从凭据库取明文注入连接器内存；令牌只出现在 Authorization 头，绝不进入 sync_logs/error/StandardItem.metadata；错误信息一律过 `redact_secrets`。
- 扩展密钥特征识别：`ghp_/gho_/ghu_/ghs_/ghr_/github_pat_` 出现在 config 中一律 400。

## X. GitHubConnector 设计

- config（无密钥）：`{owner, repo, branch?, include?=["readme","docs"], commits_limit?=20}`；owner/repo 严格白名单正则；scope 固定为"指定仓库只读"。
- HTTP：`httpx.AsyncClient`，base_url 固定 `https://api.github.com`（SSRF 白名单仅此主机；`follow_redirects=False`，URL 全部由代码拼接）；头 `Authorization: Bearer`、`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-02-28`；总超时 15s；流式读取并对单响应/单文件设上限（响应 6MB、文件 1MB、单次 100 个文件）。
- validate_config：`GET /repos/{owner}/{repo}` 验证令牌与可见性并取默认分支；401/403（非限流）→ ConnectorAuthError（创建期失败回滚并删除数据源，沿用 3.5.1 路径）；404/配置非法 → ConnectorConfigError；403 限流（remaining=0）/超时/5xx → ConnectorFetchError（临时，不删数据）。
- fetch_changes：
  1. `GET /git/trees/{branch}?recursive==1`（注意 truncated=true 时记 skipped，不假装完整）；
  2. 选取根 README* 与 `docs/` 下 md/txt/pdf/docx；blob 走 contents API（base64），二进制复用 `document_parse.parse_document`；
  3. cursor = `{"files":{path: blob_sha}, "last_commit_date": ...}`；sha 未变 → unchanged_external_ids（不重取内容）；新增/变更 → StandardItem；本轮树中缺失 → deleted_external_ids；
  4. include 含 "commits" 时：`GET /commits?since=last_commit_date&per_page=commits_limit`（上限 50），聚合成单个 `__commits__/recent.md` 条目；无新提交报 unchanged。
- external_id = 仓库内 posix 路径（前缀 `owner/repo/` 不需要，因数据源已隔离）；source_url 指向 github.com 对应文件；metadata 仅含 path/sha/size/ext（无令牌）。
- 连接器不碰 DB、不分块、不调模型；入库/删除隔离/日志全部复用 3.5.1 同步事务。
- 可测性：`GitHubConnector` 暴露类级 transport 工厂钩子，测试用 `httpx.MockTransport` 注入，全程零真实网络。

## Y. 测试（新增 tests/test_github_connector.py + 凭据测试，全离线 mock HTTP）

1. secret_crypto：加解密往返、密文不含明文、错误密钥解密失败、密钥文件权限。
2. 凭据 API：设置/更新/删除；列表只回 has_credential；DB 中只有密文；删数据源级联清除；config 携带 ghp_/github_pat_ 特征被 400。
3. 配置校验：缺令牌、owner/repo 非法、404、401/403→AuthError、限流 403→临时错误。
4. 首次同步产出 README/docs（含 base64 的 md 与二进制 docx/pdf 走 parse_document）；external_id/hash/source_url/metadata 正确且任何字段无令牌。
5. 增量：sha 未变不重取（unchanged）、变更重取、删除文件进入 deleted；commits since 增量、无新提交 unchanged。
6. 网络异常/5xx/超时 → ConnectorFetchError 且游标不前、数据保留；truncated 树计 skipped。
7. 端到端 API（MockTransport）：创建带 secret → 同步入库可检索 → 再次同步 unchanged → 撤销授权后不可同步。
8. 注册中心内置类型变为 mock/local_dir/github；全量回归。

## Z. 非目标（本轮不做）

OAuth/多账号、企业版私有域名（ghe.com 暂不支持，主机白名单仅 api.github.com）、定时/Webhook、通用 HTTP API、MCP、issues/PR/wiki/release、写入操作、真实账号冒烟（无令牌；UI 只验证类型出现与缺令牌 400）。

## AA. 3.5.2 第二批落地回填（2026-10-11）

- 新增：`app/secret_crypto.py`（Fernet 对称加密；密钥默认 `data/secret.key`，支持 `PAOS_SECRET_KEY` / `PAOS_SECRET_KEY_PATH`；密文 base64 落库，解密失败显式报错）、`app/github_connector.py`（GitHubConnector + build_transport 测试钩子）、`tests/test_github_connector.py`（14 测试）。
- 修改：`app/connectors.py`（BaseConnector/registry.create 增加 secret 注入；密钥特征新增 `gh[pousr]_` / `github_pat_`）、`app/main.py`（import 注册 github；新表 connector_credentials；创建接口可选 secret 并在校验失败时随数据源一起回滚；凭据 PUT/DELETE 接口；列表/详情 has_credential；同步时解密注入，解密失败走 ConnectorAuthError 路径）、`app/static/{index.html,connectors.js}`（github 类型提示、令牌字段仅 github 显示、设置/更新令牌按钮、令牌状态徽标）、`requirements.txt`（cryptography）、`tests/test_connectors.py`（内置类型断言更新为三种）、`README.md`。
- 关键实现点：令牌绝不进 config_json/cursor/metadata/日志（只进 Authorization 头）；base 主机硬编码 api.github.com、不跟随重定向、路径/分支 URL 编码；超时 15s、响应 6MB/文件 1MB/100 文件/50 提交上限；blob SHA 增量（未变不重取），删除由本轮 tree 驱动；commits 用 last_commit_date 走 since；限流 403 与 401/403 权限错误区分（临时 vs 撤销）。
- 测试：凭据加解密往返/换密钥失败；凭据 API 只回布尔、库中仅密文、级联删除、令牌混入 config 被 400；GitHub 配置校验四类错误；首次/增量/删除/提交增量；5xx 游标不前；端到端同步入分块表可检索、撤销后 409、删源清凭据。全量 **111 passed, 1 skipped**。
- UI 冒烟：connector-types 含 github；令牌字段对 mock/local 隐藏、对 github 显示；缺令牌创建被 400 拒绝且无残留；零非预期 console 错误。真实库三张连接器表 + 凭据表 + 文档表均为 0。
- 下一轮 3.5.3 计划：通用 HTTP API 连接器（配置化请求与字段映射、域名白名单/SSRF）、手动/定时调度、授权管理增强；OAuth/企业版域名/issues/PR 仍不做。

# 第六轮实施细则：3.5.3（第一批）通用 HTTP API 只读连接器（2026-10-11）

> 范围：新增 `HttpApiConnector`（kind=`http_api`），把**用户显式配置**的一个 HTTPS GET 接口返回的 JSON 列表，按字段映射标准化为 StandardItem，复用既有同步事务/凭据库/删除隔离。仅手动同步。定时调度、分页/Webhook/POST 体/请求体写操作留下一批或后续。

## AB. 配置与映射

- config（全部非密钥；令牌仍走 connector_credentials）：
  - `url`（必填）：单次 GET 的完整接口地址，仅允许 `https`；`http` 仅在显式 `allow_localhost=true` 且主机为 loopback 时允许。
  - `items_path`（可选，默认响应体即数组）：点分路径定位条目数组，如 `data.items`。
  - `fields`（可选）：字段映射，默认 `{external_id:"id", title:"title", content:"content", updated_at:"updated_at", source_url:"url"}`；external_id 与 content 必填字段取不到时跳过该条。
  - `auth_header_name`（可选，默认 `Authorization`）：有令牌时发送 `<name>: Bearer <token>`。
  - `allow_localhost`（可选，默认 false）：仅放行 127.0.0.1/::1，用于本机自用接口。
- 支持的静态 query 参数直接写在 `url` 中；不支持任意自定义静态头（避免放密钥），密钥仍一律被 `assert_config_has_no_secrets` 拒绝。

## AC. SSRF 与传输安全

- validate_config：URL 解析 + DNS 解析（getaddrinfo）逐 IP 判定：仅 global 地址可连；默认拒绝 loopback/private/link-local（含 169.254.169.254 云元数据）/multicast/reserved/unspecified；`allow_localhost` 只额外放行 loopback。
- fetch_changes 前再次校验（配置可能被改）；`follow_redirects=False`（不接受任何跨主机跳转）；超时 15s；响应体上限 6MB；条目上限 200；单条内容受 StandardItem 200k 限制。
- 残余风险（写入 README）：域名在“校验—连接”之间理论上可重绑定（TOCTOU）；本机单用户应用可接受，后续可用自定义 transport 固定 IP 彻底消除。
- MockTransport 测试钩子同 GitHub 连接器（`build_transport`），全离线测试；对字面 IP 主机直接判定，不依赖 DNS。

## AD. 同步语义与错误映射

- cursor = `{"ids":{external_id: content_hash}}`：本轮全量拉取后在连接器内做差分——未变→unchanged_external_ids；新增/变更→items；上轮有本轮无→deleted_external_ids（复用主同步事务，删除只影响本源）。
- 401/403（非限流）→ ConnectorAuthError（置 revoked）；429/5xx/超时/DNS 失败/非 2xx→ ConnectorFetchError（不删数据、游标不前）；validate 阶段 404 → ConnectorConfigError。
- 不跟随条目内 URL 抓正文（第一批仅使用响应内联字段），不调模型、不写记忆/画像。

## AE. 测试（tests/test_httpapi_connector.py，全离线）

1. 配置校验：非 http(s) scheme、缺 url、items_path 不存在/非数组、字段映射缺必填、私网 IP/云元数据 IP/字面内网地址拒绝；allow_localhost 仅对 loopback 放开；https 公网域名经 mock 通过。
2. 凭据：无令牌也可同步（公开接口）；有令牌发送 `Authorization: Bearer …`；401→AuthError。
3. 首次/增量：首次全部 items；二次无变化全 unchanged；改一条 content 仅该条更新；删一条进 deleted；非法条目（缺 id/content）跳过不阻断。
4. 安全：3xx 跳转不跟随；429/5xx/超时→FetchError 且游标不前；响应体超限拒绝；超 200 条截断；令牌不出现在任何 item/错误信息。
5. 端到端 API：创建 http_api 数据源（MockTransport）→同步入库可检索→再次 unchanged→删数据源清理；注册中心内置四类连接器。

## AF. 非目标（本轮不做）

分页/游标参数、POST/请求体、自定义鉴权方案（query token/OAuth）、每条目二次抓取、定时调度/Webhook、DNS 重绑定的传输层彻底固定（下一批调度时一并做）。

## AG. 3.5.3 第一批落地回填（2026-10-11）

- 新增：`app/httpapi_connector.py`（HttpApiConnector + `build_transport`/`resolve_ips` 测试钩子）、`tests/test_httpapi_connector.py`（10 测试）。
- 修改：`app/main.py`（import 触发 http_api 自注册）、`app/connectors.py` 无需改动（复用 secret 注入/密钥拒绝/StandardItem）、`app/static/{index.html,connectors.js}`（http_api 配置提示、令牌字段对 http_api 也显示、设置/更新令牌按钮与徽标）、`tests/test_connectors.py`（内置类型断言更新为四类）、`README.md`。
- 关键实现点：仅 https（loopback 需显式 allow_localhost 且只放行 127.0.0.1/::1/localhost）；字面 IP 直接分类、域名走可打桩的 getaddrinfo；拒绝 private/link-local（含 169.254.169.254）/multicast/reserved/unspecified；不跟随重定向；超时 15s、响应 6MB、200 条上限；items_path 点分取数组、自定义 fields 必须显式映射 external_id/content；cursor={ids:{id:hash}} 连接器内差分；401/403→revoked，429/5xx/超时/DNS 失败/3xx/非 JSON→临时错误不删数据；公开接口允许无令牌，有令牌以 `Authorization: Bearer`（可配 header 名）发送。
- 已知残余风险（已写入 README）：DNS 校验与实际连接之间理论上可重绑定（TOCTOU）；本机单用户可接受，下一批调度时用“固定解析 IP 的自定义 transport”彻底消除。
- 测试：SSRF 各地址段/坏 scheme/坏映射/坏 header 名拒绝、allow_localhost 仅 loopback、DNS 失败临时错误、401/429/503/3xx/超大/非 JSON/超时分类、首次+增量+更新+删除、items_path 嵌套与非法条目跳过、200 条上限、端到端入分块表可检索、令牌只在 header 不落 config/日志、公开端点免凭据、删源清理。全量 **121 passed, 1 skipped**。
- UI 冒烟：四类连接器出现；http_api 显示令牌字段与提示；云元数据 URL 创建被 400 拒绝且无残留；零非预期 console 错误。真实库连接器/凭据/文档表均为 0。
- 下一批（3.5.3 第二批）计划：手动/定时调度（间隔、下次同步时间、失败退避，仍默认关闭）、分页/游标参数、传输层固定解析 IP 消除 DNS 重绑定；授权管理增强（按源撤销已具备，考虑同步范围只读化校验）。POST/请求体、OAuth、每条目二次抓取仍不做。

# 第七轮实施细则：3.5.3（第二批）定时调度（2026-10-12）

> 范围：数据源**手动/定时同步调度**，默认全部关闭、用户逐源显式开启；单一后台轮询任务、失败指数退避、状态机约束（仅 active 且 enabled 且到期才同步）。不做 Webhook、不做多进程分布式锁（本机单进程单用户）。同时尝试收敛 HTTP API 连接器的 DNS 重绑定窗口（解析后固定 IP 建连，SNI/Host 保持原域名；若底层库不支持则保留取数前二次校验并在 README 标注）。

## AH. 数据模型（纯增量迁移，幂等 ALTER）

- `data_sources` 新增列：`sync_enabled INTEGER NOT NULL DEFAULT 0`、`sync_interval_minutes INTEGER NOT NULL DEFAULT 60`、`next_sync_at TEXT`、`consecutive_failures INTEGER NOT NULL DEFAULT 0`。
- 间隔范围 1..10080（1 分钟–7 天），默认 60；`sync_enabled=0` 时 `next_sync_at=NULL`。
- 旧数据全部默认不调度，行为与现状完全一致。

## AI. 调度器（app/connector_scheduler.py）

- `ConnectorScheduler(due_provider, sync_runner, poll_seconds=30)`：单个 asyncio 任务循环；`tick()` 取出到期源**串行**执行（避免并发打爆第三方限流）；进程内锁保证同轮不重叠；start/stop 幂等。
- 到期判定（SQL）：`status='active' AND sync_enabled=1 AND next_sync_at IS NOT NULL AND next_sync_at<=:now`。
- main startup 启动、shutdown 优雅停止；TestClient 触发生命周期时因默认无 enabled 源不会产生外部请求。
- 排程规则（可单测的纯函数）：成功 → failures=0、`next=now+interval`；失败 → failures+=1、`next=now+min(interval*2^(failures-1), 1440)`（上限 24h）；撤销/暂停/删除/禁用 → 不再被选中；手动同步成功同样重置退避。

## AJ. API（沿用现有命名）

- `POST /api/data-sources/{id}/schedule` body `{enabled:bool, interval_minutes?:int}`：校验范围与状态（revoked 源需先恢复？允许设置但 revoked 不会被调度）；设置 next_sync_at。
- `PATCH /api/data-sources/{id}` 同步支持这两个字段；创建接口可选 `sync_enabled/sync_interval_minutes`。
- 列表/详情返回这四个调度字段。

## AK. 测试（tests/test_connector_schedule.py，全离线）

1. 旧库迁移：init_db 后旧列存在且默认值正确、旧源 enabled=0。
2. schedule 校验：interval 越界、非 bool、404；开启后 next_sync_at 落库且≈now+interval；关闭后 next_sync_at=NULL。
3. 手动同步成功后排程/重置失败计数；mock 连接器模拟 fetch 失败两次：failures 递增、next_sync_at 按退避推迟（直接调用纯函数与 tick，不睡真实时间）。
4. tick 选择器：active+enabled+到期 才执行；paused/revoked/disabled/未到期 不执行；执行后写 sync_logs。
5. start/stop 幂等、无 enabled 源时空转。
6. 连接器行为回归（全量 pytest）。

## AL. 非目标

分布式/多 worker 锁、Webhook、cron 表达式（固定间隔足够）、调度并发同步多个源、自动开启任何现有源、UI 复杂编排（仅开关+间隔输入+下次同步时间展示）。

## AM. 3.5.3 第二批落地回填（2026-10-12）

- 新增：`app/connector_scheduler.py`（纯排程函数 `next_run_after_success/next_run_after_failure/normalise_interval`、`ConnectorScheduler` 单 asyncio 任务串行 tick、start/stop 幂等、poll 默认 30s）、`tests/test_connector_schedule.py`（9 测试）。
- 修改：`app/main.py`（`data_sources` 新表 DDL 加 4 列 + 幂等迁移 `_migrate_data_sources`，旧源默认 enabled=0；同步端点重构为 `_run_source_sync` 核心 + HTTP 薄封装，成功重置 failures 并按间隔排程、失败指数退避（interval·2^(n-1)，上限 24h）、auth 撤销清空排程；`POST /api/data-sources/{id}/schedule`；创建/PATCH 支持调度字段；暂停/撤销清 next_sync_at、恢复时对已 opt-in 源重新排程；startup 启动/shutdown 停止调度器）、`app/static/connectors.js`（scheduleLine 辅助 + 开启定时/定时设置按钮 + 下次同步与连续失败展示）、`README.md`。
- 关键语义：调度**默认全关**、逐源 opt-in；仅 `status=active AND sync_enabled=1 AND next_sync_at<=now` 被选中，串行执行避免打爆第三方限流；失败后下次按退避推迟，故失败源不会立即再次到期；间隔范围 1..10080 分钟；enabled 严格布尔（拒绝字符串宽松转换）。
- 测试：旧库无新列迁移后默认值正确；schedule 校验/排程/关闭、revoked 不能开启且 409、pause/resume 重新排程、PATCH 间隔重排；退避数学（1→1h、2→2h、3→4h、封顶 24h）与边界；tick 串行/空转/start-stop 幂等；真实同步失败两次退避加倍、成功后归零重排；disabled 永不到期。全量 **130 passed, 1 skipped**。
- API 冒烟（真实服务）：开启 30 分钟排程落库、interval=0 与 enabled="yes" 均 422、手动同步成功后 failures=0、关闭后 next 清空、删除清理。UI 冒烟：未开启标签→开启定时（prompt 60）→展示“每 60 分钟/下次”→输入 0 关闭→删除，零 console 错误。
- 错误留档：`docs/errors/2026-10-12-connectors-template-nested-ternary.md`（前端模板内嵌套三元致整个 connectors.js 失效；纪律：改 JS 后先 `node --check`）。
- 下一批可选：传输层固定解析 IP 消除 DNS 重绑定、分页/游标参数；或回到 1.3.2 行为/习惯统计。Webhook/OAuth/POST 请求体/多进程分布式锁仍不做。
