# 阶段 4（Personal AI OS 1.3）：个性化分析引擎
> 2026-10-12：1.3.4-E 安全与质量最终验收已完成（统计亚秒半开边界、读快照、聚合白名单、反馈/结果写入与删除竞争加固；319 passed / 1 skipped，compileall 与 7 个 JS 文件检查通过，临时库 Edge 冒烟通过，真实开发库 SHA-256 不变）。在本地 loopback/单实例/单用户/无登录前提下具备 1.3.4 阶段收口条件；不进入 1.3.5。
> 2026-10-12：1.3.4-D 效果统计与质量评估已完成（只读统计服务、`GET /api/recommendation-statistics`、智能建议页统计面板、当前反馈/结果与生命周期事件分离、评级占比、零分母/样本不足标记、隐私聚合与 Edge 临时库冒烟）；未调整推荐策略，未写入记忆/画像，未进入 E 批。最终全量实测 `310 passed, 1 skipped, 5 warnings`，真实开发库 SHA-256 不变。
> 2026-10-12：1.3.4-C 反馈 API 与智能建议页面已完成（6 个反馈/结果接口、页面反馈/结果表单、保存前确认、409 保留输入、撤销采纳后结果只读、XSS/CSRF/隔离测试与 Edge 临时库冒烟）；未实现效果统计或推荐策略反馈闭环。最终全量实测 `300 passed, 1 skipped, 5 warnings`，真实开发库 SHA-256 不变。等待批准 1.3.4-D。
> 2026-10-12：1.3.4-B 反馈与结果服务层已完成（新增两张表、独立服务、同事务行为事件、级联删除与 22 项专项测试）；未新增 API/前端，未实现统计。最终全量实测 `273 passed, 1 skipped, 5 warnings`，真实开发库哈希不变。等待批准 1.3.4-C。
> 2026-10-12：1.3.4-A 建议反馈与结果追踪的现状审计与数据模型设计已完成（仅文档，无业务代码/数据库迁移）；设计见 `phase-4-1.3.4-recommendation-feedback.md`。基线复跑 `251 passed, 1 skipped, 5 warnings`，等待批准 1.3.4-B 服务层。

> 2026-10-12：1.3.3-A/B/C/D/E 已全部完成并收口；E 批安全质量验收实测 251 passed / 1 skipped，Edge headless 临时库冒烟通过。未开始 1.3.4，见 `phase-4-1.3.3-personalized-recommendations.md`。
> 2026-10-12：1.3.3-A 现状审计与实施计划已完成（未改业务代码），见 `phase-4-1.3.3-personalized-recommendations.md`；基线复跑 161 passed / 1 skipped，等待批准 B 批（建议领域模型与存储）。

> 2026-10-12：1.3.2-A/B/C/D 已全部完成：行为事件、确定性统计、周期对比、只读分析 API 与行为洞察页面形成闭环，见 `phase-4-1.3.2-behavior-analytics.md`；最新全量回归 `161 passed, 1 skipped`，Edge headless 冒烟通过。下一步为待批准的 1.3.3 个性化建议引擎。
> 状态：**第一轮 1.3.1 个人画像模块 — 已完成**（2026-10-11）：后端 7 接口 + `app/profile_propose.py` + 前端「个人画像」页，新增 14 个测试，全量 **74 passed**，Playwright 无头冒烟通过，真实库迁移无损。下一轮为 1.3.2 行为/习惯统计。
> 前置检查：阶段 2 长期记忆引擎（迭代 01–05）与阶段 3 个人数据中心（3.1–3.4）均已实际完成；基线 `pytest -q` = **60 passed**（2026-10-11 19:54 复跑确认）。
> 本轮范围纪律：**只做 1.3.1 个人画像**，不做习惯/趋势统计（1.3.2）、建议引擎（1.3.3）、反馈系统（1.3.4）、仪表盘聚合（1.3.5）；不引入向量库/多 Agent/外部平台，不改写对话检索链路。

## 〇、开始前检查结论（实际代码核对）

| 检查项 | 结论 |
| --- | --- |
| 长期记忆候选提取/审核/纠错/删除/跨会话检索 | 已存在（memories + memory_candidates + memory_revisions，`/api/memory-candidates/*`、`/api/memories/*`） |
| 数据库迁移机制 | `init_db()` 中 `CREATE TABLE IF NOT EXISTS` + `_migrate_memories/_migrate_documents` 幂等 ALTER，启动时执行，保留旧数据 |
| 文档检索 / 项目目标任务 | 已存在（documents/document_chunks、projects/project_events/goals/tasks，retriever 三路检索） |
| 现有测试 | tests/ 5 个文件，60 项全绿 |
| 多用户 | **不支持**，单用户本地应用，无认证；"越权/隔离"测试本轮替换为"非法输入 + 仅 127.0.0.1 绑定"核对，并在 README 风险中注明 |
| 现有 `/api/profile` | 单条自由文本档案（display_name/goals/preferences），**保留不破坏**；结构化画像另起新表与新页面 |

## 一、1.3 全貌（5 个迭代）

| 迭代 | 交付 | 验收 |
| --- | --- | --- |
| **1.3.1（本轮）** | 个人画像：结构化条目、事实/推测分级、来源追踪、审核动作、模型提议（仅落待确认） | 可生成/查看/编辑/审核/删除画像；推测不会自动变事实；来源可追溯；模型不可用时 CRUD 不受影响 |
| 1.3.2 | 行为事件与习惯/趋势统计（程序统计，非模型猜测） | 基于真实历史数据产出统计结果 |
| 1.3.3 | 个性化建议引擎（依据/优先级/收益/风险/置信度） | 建议带证据与风险说明 |
| 1.3.4 | 建议反馈（采纳/拒绝/评价/结果记录，区分临时与长期） | 反馈可记录、画像偏好可更新 |
| 1.3.5 | 个人洞察仪表盘 | 画像、趋势、建议、历史变化一屏查看 |

核心原则（贯穿 1.3）：统计交给程序、解释交给模型；每条画像/建议必须有依据；画像可追溯；先分析后行动（本阶段只生成建议，不执行操作）。

## 二、第一轮（1.3.1）数据模型

纯新增表，`CREATE TABLE IF NOT EXISTS`，**不动任何旧表旧数据**。

### 2.1 `profile_items`（结构化个人画像条目）

| 字段 | 类型/约束 | 用途 |
| --- | --- | --- |
| id | INTEGER PK AUTOINCREMENT | 稳定 ID |
| category | TEXT NOT NULL，CHECK ∈ `basic,skill,interest,goal,preference,observation,inference` | 画像类别（基础/技能/兴趣/目标/偏好/行为观察/待确认推测） |
| title | TEXT NOT NULL DEFAULT ''，≤200 字 | 短标签（如技能名），可空 |
| content | TEXT NOT NULL，1–2000 字 | 条目正文 |
| meta | TEXT NOT NULL DEFAULT '{}' | 结构化附加信息 JSON（如 `{"level":"advanced","related_project_ids":[1]}`），落库前 JSON 序列化，整体 ≤1000 字 |
| status | TEXT NOT NULL DEFAULT 'pending'，CHECK ∈ `pending,confirmed,expired,rejected` | 待确认/已确认/已过期/已拒绝 |
| is_user_stated | INTEGER NOT NULL DEFAULT 0 | 是否用户明确陈述（事实）；模型提议恒为 0，用户确认时置 1 |
| confidence | REAL NOT NULL DEFAULT 0.5，0–1 | 置信程度 |
| source_type | TEXT NOT NULL DEFAULT 'manual'，CHECK ∈ `manual,memory,document,project,goal,task,model` | 来源类型 |
| source_id | INTEGER 可空 | 关联记录 ID（多态关联，不建硬外键，源记录删除后 UI 优雅降级） |
| source_detail | TEXT NOT NULL DEFAULT ''，≤500 字 | 来源补充说明（如片段编号、证据摘要），保证可追溯 |
| valid_from / valid_until | TEXT 可空（YYYY-MM-DD） | 适用时间范围 |
| created_at / updated_at / reviewed_at | TEXT | 创建 / 最近更新 / 审核时间（UTC ISO） |

索引：`(status)`、`(category, status)`。

**事实分级规则（红线）**：
- 手动新增 = 用户明确陈述 → `status='confirmed', is_user_stated=1`。
- 模型提议 = `status='pending', is_user_stated=0, source_type='model'`；**任何路径都不得自动置为 confirmed**，只有用户显式确认才可提升为事实。
- 拒绝 pending → `rejected`（保留留痕，默认列表不返回）；确认后可标记 `expired`；删除为物理删除。
- 手动接口不接受 `category='inference'`（推测只能来自提议接口）。

### 2.2 `profile_item_revisions`（条目级版本/审计，复刻 memory_revisions 模式）

| 字段 | 用途 |
| --- | --- |
| id | 主键 |
| item_id | INTEGER，外键 → profile_items(id) ON DELETE CASCADE |
| event | create/update/confirm/reject/expire/propose/delete |
| detail | TEXT（JSON 或文本，记录变更摘要） |
| created_at | 时间 |

物理删除条目之前，先写一条 `item_id=NULL` 的 deleted 审计（避免级联抹掉删除痕迹），与记忆模块做法一致。

## 三、后端模块与 API

### 3.1 模块拆分

- `app/profile_propose.py`（新增）：纯函数 `parse_proposals(raw_text)`（严格解析模型 JSON、白名单过滤、截断/夹紧置信度、限最多 10 条、剔除空内容）+ `build_proposal_payload(memories, goals, profile)`（只组装最小必要上下文：已确认记忆、进行中目标、自由文本档案）。不直接访问网络。
- `app/main.py`：新增表 DDL（并入 `init_db` 的 executescript）、Pydantic 模型、路由；复用 `db()/now_iso()`；新增 `_log_item_revision()` 与密钥正则 `_contains_secret()`。
- 不修改对话 `/api/chat` 与 retriever（画像进入回答上下文留给 1.3.3）。

### 3.2 接口（命名与现有 `/api/memories`、`/api/projects` 风格一致）

| 方法/路径 | 行为 |
| --- | --- |
| `GET /api/profile-items?status=&category=` | 当前画像；默认不返回 rejected，可用 status 显式查 |
| `POST /api/profile-items` | 手动新增（强制 confirmed + is_user_stated=1；inference 类别 400） |
| `PATCH /api/profile-items/{id}` | 编辑 title/content/meta/confidence/category/日期；状态不变；404/校验 |
| `POST /api/profile-items/{id}/confirm` | 仅 pending → confirmed，置 is_user_stated=1、reviewed_at；其他状态 409 |
| `POST /api/profile-items/{id}/reject` | 仅 pending → rejected + reviewed_at；其他 409 |
| `POST /api/profile-items/{id}/expire` | 仅 confirmed → expired，valid_until=今天；其他 409 |
| `DELETE /api/profile-items/{id}` | 物理删除 + 删除前写审计；404 |
| `GET /api/profile-items/{id}/history` | 条目 + 修订历史；404 |
| `POST /api/profile-items/propose` | 模型提议：读最小上下文 → LLM（结构化 JSON）→ 全部落 **pending/inference**；无 `LLM_API_KEY` 返回 **400**（与 `/api/memory-candidates/extract` 一致）；测试全程 mock，不联网、不产生费用 |

### 3.3 输入校验与错误码

- 空白 content/title（给了非空但全空白）→ 422；非法枚举（category/source_type/status 转移）→ 400/409；未知 ID → 404。
- confidence 越界、meta 非对象、日期格式不符 → 422。
- **密钥红线**：content/title/source_detail 命中 `sk-…`、`(api_key|secret|password|token)\s*[:=]`、`Bearer …`、AWS `AKIA…` 等模式 → 400 拒绝，提示不要把口令写入画像。

## 四、安全与隐私（第一版内建）

- 只读库内已授权数据；不扫描磁盘、不接外部账号；服务仅绑 127.0.0.1。
- 提议接口只发送已确认记忆/进行中目标/档案摘要（最小化），不发送文档全文；模型输入输出均按不可信数据包裹，提示词注入文本只会成为待审条目内容，系统无工具可执行。
- 密钥模式拒绝写入；前端渲染统一走现有 `esc()`，不引入 innerHTML 未转义拼接。
- 删除即物理删除条目（修订级联 + 删除前审计留痕）。
- 源记录（记忆/文档/任务）被删除后画像条目不联动消失（多态关联无法建外键），UI 仅展示来源类型与 ID；该取舍写入 README 风险。

## 五、前端（新增独立页面，不改写旧页面）

- 侧栏新增「个人画像」（`data-page="persona"`），新增 `app/static/persona.js`（复刻 work.js 的加载/绑定方式）；index.html 内联 `navigate()` 增加 `persona` 分支。
- 左栏：按类别分组展示条目；徽章区分「已确认事实 / 待确认推测 / 已过期 / 已拒绝」；显示来源、置信度、更新时间；按状态给出 编辑 / 确认 / 拒绝 / 标记过期 / 删除 按钮。
- 右栏：手动新增表单（类别下拉不含"待确认推测"）+「用模型从已有资料生成待确认推测」按钮（明确标注：这些只是猜测，需你确认）；`<details>` 折叠已拒绝历史。
- 空状态 / 加载中 / 错误提示齐全；沿用现有配色与组件样式。
- 旧「个人档案」页（自由文本）原样保留。

## 六、测试（pytest，新增 tests/test_profile_items.py，全程不联网）

1. 新用户画像为空（默认不含 rejected）。
2. 手动新增即 confirmed + is_user_stated=1；手动传 inference 被拒。
3. 提议（mock LLM）落 pending/inference/is_user_stated=0，与确认事实在列表中状态可区分；模型返回畸形 JSON 时不崩、返回空。
4. 编辑改内容并写 revision；确认/拒绝/过期状态转移正确，错误状态 409。
5. 删除后条目与历史 404；审计中留有 deleted 记录。
6. 来源字段（source_type/source_id/source_detail）正确存取。
7. 密钥模式被拒（400）；空白正文/越界 confidence/非法类别 → 422/400。
8. 迁移兼容：旧库（档案/记忆/文档/任务数据）上连续两次 `init_db()` 不报错、旧数据原样在、新表可正常写入。
9. 降级：无 LLM_API_KEY 时 propose 返回 400，而画像 CRUD 全部正常；注入文本（"忽略以上指令…"）经提议仅成为待审数据，不产生任何执行副作用。

## 七、实施步骤（小步快走，每步跑测试）

1. 写本计划文档（本文件）。
2. 加表 DDL + 迁移幂等性，先写测试 1/8 跑通。
3. Pydantic 模型 + 密钥校验 + CRUD/审核/历史路由与测试 2/4/5/6/7。
4. `profile_propose.py` + propose 路由 + mock 测试 3/9。
5. persona.js + index.html 导航/区块，浏览器冒烟（Playwright 无头脚本验证加载/无 console 错误/一次审核交互）。
6. 更新 README（接口、启动、测试、风险），回填本计划状态行；如遇错误按约定写 docs/errors/。

## 八、明确不做（本轮）

- 不做 behavior_events/insights/recommendations/feedback 等 1.3.2–1.3.5 表与统计逻辑。
- 不把画像接入 /api/chat 上下文、不做画像驱动的回答。
- 不做多用户与权限体系；不做画像导入导出（留待 1.6）。

## 九、落地回填（实施后实际结果，2026-10-11）

- 新增文件：`app/profile_propose.py`（最小上下文组装 + JSON 严格解析/夹紧/去重/密钥过滤 + 模型调用）、`app/static/persona.js`、`tests/test_profile_items.py`（14 项）、本计划文档。
- 修改文件：`app/main.py`（init_db 增 `profile_items/profile_item_revisions` 两表、画像 Pydantic 模型与 7 条路由、propose 路由）、`app/static/index.html`（侧栏入口、画像页区块、navigate 分支、脚本引用）、`README.md`。
- 数据库：纯新增两表（CREATE IF NOT EXISTS），无对旧表的 ALTER；启动自动迁移，对真实库执行后旧数据计数不变。
- 与计划的偏差（已回写）：复用了 main.py 既有的 `_validate_iso_date(value, field)`（非法日期按项目既有约定返回 400，而非计划初稿写的 422），画像代码未另起同名函数（见 errors 留档）。
- 冒烟在真实库产生过 1 条删除审计残留，因画像表尚无用户真实数据，已清理该测试痕迹。

