# Personal AI OS MVP

本地优先的个人 AI 操作系统 MVP。应用运行在本机，使用 FastAPI 提供 API，前端为 Vue 3 + Vite 构建后由 FastAPI 静态挂载，使用 SQLite 保存个人档案、长期记忆、文档、项目/任务、连接器、行为分析、建议反馈和提醒数据。

默认不配置大模型 Key 也可本地演示；配置 OpenAI 兼容接口后，可使用云端聊天、记忆候选提取、画像候选生成和显式开启的建议润色。

> 自动提醒调度已接入 FastAPI lifespan，但默认通过 `REMINDER_SCHEDULER_ENABLED=0` 关闭；启用后也只执行本地 SQLite 状态调度。提醒到期会在同一事务写入通知 outbox；通知消费器由 `NOTIFICATION_DELIVERY_ENABLED=0` 默认关闭，显式启用后本阶段也只运行本地 `fake_test` 测试渠道，**不会发送邮件、短信、Webhook 或桌面通知**。设计见 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md`。

## 1. 功能状态

### 已实现

- 个人档案与本机 Web UI。
- 长期记忆：候选提取、人工审核、重复/冲突提示、过期、修订历史、带引用检索。
- 文档知识库：PDF、DOCX、Markdown、TXT 解析；哈希去重、分块、来源页码/标题和片段级检索。
- 项目、目标、任务、对话和审批记录；项目/目标/任务在 Web UI 内可新建、编辑、改状态、删除，项目可追加进展/决策/问题事件。
- 行为事件、确定性分析和周期对比。
- 个人画像条目、证据驱动建议、建议反馈和结果记录；画像页可新增/确认/拒绝/过期条目并查看修订历史，建议页可登记实际效果（执行状态/评级/指标观察），并提供只读统计面板与生成历史。
- 知识库为**混合检索**：FTS5 全文 + 词法评分 + **向量语义**（千问 text-embedding-v4，1024 维，相似度 ≥0.45 才加成），同义改写也能召回；`/api/embeddings/status` 看覆盖率，`/api/embeddings/reindex` 分批回填。
- 本地目录、GitHub、通用 HTTP API 只读连接器；连接器同步日志和逐源 opt-in 定时同步。
- connector token 使用 Fernet 加密存储，普通连接器配置中禁止明文密钥；数据源页可设置/更换/清除凭据（只写不回显），新建 GitHub 源可直接填 PAT。
- 提醒设置、提醒存储、规则、安静时段、状态机、恢复与手动 `run_tick()` 调度核心；提醒中心可按状态执行知悉/延期（可选分钟数，显示下次唤醒时间）/完成/忽略/取消，行为洞察页提供 7/30 天等长周期对比表。
- 提醒到期通知 outbox：事件与渠道投递记录分离、稳定幂等键、指数退避、死信、running 恢复和多进程条件领取。
- `fake_test` 内存通知渠道与默认关闭的单线程消费服务；不提供真实外部通知。
- SQLite 跨进程调度租约、TTL、heartbeat/release 和 fencing token 写入保护。
- loopback Host 校验、浏览器跨站状态变更保护、模型/文档内容提示注入隔离。

### 未完成或受限

- 自动提醒生命周期：已接入 FastAPI lifespan，但通过 `REMINDER_SCHEDULER_ENABLED=0` 默认关闭。
- 通知消费基础设施：事件/投递记录、重试、死信、恢复和 fake channel 已实现；消费器通过 `NOTIFICATION_DELIVERY_ENABLED=0` 默认关闭。
- 真实外部通知发送器：未实现；当前代码没有邮件、短信、Webhook、桌面或云通知渠道加载器。
- 用户登录/多用户权限：未实现；应用只能绑定 loopback，不应暴露公网。
- 独立迁移框架/版本表：未引入；当前依赖 `init_db()` 中的幂等建表和增量辅助迁移。
- PostgreSQL/pgvector：未迁移；检索层保留替换接缝。
- 上传原始文件当前不以独立磁盘文件形式长期保存，主要保存解析文本和元数据。
- `app/main.py` 仍承担路由、schema 和部分业务流程，后续应按领域小步拆分。

## 2. 技术栈

- Python 3.11+（当前开发虚拟环境实测 Python 3.13.9）
- FastAPI + Uvicorn
- SQLite
- 前端：Vue 3 + Vite 6 + Tailwind CSS（源码在 `frontend/src/`，构建产物输出到 `app/static/`）
- Pydantic
- httpx
- pypdf、python-docx
- cryptography/Fernet
- pytest

## 3. 目录结构

```text
personal-ai-os-mvp/
├── AGENTS.md                  # Codex/AI 协作规则和安全红线
├── README.md
├── pyproject.toml             # Python 版本与 pytest 配置
├── requirements.txt           # 运行时依赖
├── requirements-dev.txt       # 测试依赖
├── .env.example
├── .gitignore
├── run.bat                    # Windows 启动脚本
├── app/
│   ├── main.py                # FastAPI、HTTP 路由、schema/迁移、生命周期
│   ├── model_config.py        # OpenAI 兼容模型配置
│   ├── reminders.py           # 提醒领域、规则、状态机
│   ├── reminder_scheduler.py  # 确定性同步 tick、边界续租和 fencing 写入
│   ├── reminder_scheduler_service.py  # opt-in asyncio 生命周期与单线程执行器
│   ├── reminder_scheduler_lock.py
│   ├── notification_delivery.py          # 通知 outbox、状态机、重试、fake_test 渠道
│   ├── notification_delivery_service.py  # 默认关闭的单线程 fake 消费服务
│   ├── connector_scheduler.py # 数据源同步 asyncio 循环
│   ├── *_connector.py         # local/GitHub/HTTP connector
│   ├── secret_crypto.py
│   └── static/                # Vite 构建产物（由 frontend/ 生成，勿手改）
├── frontend/
│   ├── src/                   # Vue 3 源码（App.vue + pages/）
│   ├── vite.config.ts         # base=/static/，outDir=../app/static
│   └── package.json
├── tests/
│   ├── conftest.py            # 强制临时数据库/密钥隔离
│   └── test_*.py
├── data/                      # 本地运行数据（默认不提交）
└── docs/
    ├── architecture.md
    ├── module-responsibilities.md
    ├── database.md
    ├── configuration.md
    ├── development-workflow.md
    ├── current-task.md
    ├── progress.md
    ├── decisions.md
    ├── testing.md
    ├── errors/
    └── plans/
```

完整模块边界见 [docs/module-responsibilities.md](docs/module-responsibilities.md)。

## 4. 环境准备

在项目根目录打开 PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

如果虚拟环境已存在，可直接执行后两条安装命令。不要把 `.venv/` 提交或复制到其他机器复用。

前端（仅修改 `frontend/src/` 后需要，日常启动不需要）：

```powershell
cd frontend
npm install
npm run build          # 输出到 ../app/static，index.html + assets/<hash>.js|css
```

`app/static/index.html` 与 `app/static/assets/index-*.js|css` 都是构建产物，不要手改，也不要往 `app/static/` 放任何需要保留的文件：`vite.config.ts` 使用 `base=/static/` 与 `emptyOutDir: true`，每次构建都会先清空该目录再写入当前产物，因此不会残留旧 hash 文件。

页面冒烟（可选；必须先起一个指向临时库的实例，切勿指向 `data/personal_ai_os.db`）：

```powershell
# 终端 1：临时库 + 独立端口
$env:DATABASE_PATH="temp/e2e-work.db"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18767

# 终端 2：在 frontend 下执行，才能解析 node_modules 里的 playwright
cd frontend
node e2e_work.mjs http://127.0.0.1:18767
```

脚本会创建并删除一个项目、一个目标、一个任务，预期输出 `36/36 passed`；跑完删掉 `temp/e2e-work.db*`。

其余页面的冒烟脚本同构：`e2e_insights.mjs`（建议统计，配 `scripts/e2e/seed_insights.py`，端口示例 18768）、`e2e_profile.mjs`（画像条目，配 `scripts/e2e/seed_profile.py`，18769）、`e2e_snooze.mjs`（提醒延期与周期对比，配 `scripts/e2e/seed_snooze.py`，18770）、`e2e_credential.mjs`（连接器凭据，配 `scripts/e2e/seed_credential.py`，18771）。都只允许指向临时库实例。

建议洞察冒烟需要先播种（1 条 accepted + 1 条 pending 建议）：

```powershell
$env:SEED_DB_PATH="temp/e2e-rec.db"
.\.venv\Scripts\python.exe temp\seed_insights.py
$env:DATABASE_PATH="temp/e2e-rec.db"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18768
# 另一个终端
cd frontend && node e2e_insights.mjs http://127.0.0.1:18768
```

预期输出 `29/29 passed`；跑完删掉 `temp/e2e-rec.db*` 与 `temp/e2e.key`。

## 5. 配置

复制模板并按需填写：

```powershell
Copy-Item .env.example .env
```

`.env.example` 只包含变量名和非敏感示例：

| 变量 | 说明 |
|---|---|
| `LLM_BASE_URL` | OpenAI 兼容 API 基础 URL，默认 `https://api.openai.com/v1` |
| `LLM_API_KEY` | 云端模型 Key；留空时使用本地演示聊天 |
| `LLM_MODEL` | 模型名，默认 `gpt-4o-mini` |
| `APP_HOST` | `run.bat` 监听地址，默认 `127.0.0.1` |
| `APP_PORT` | `run.bat` 监听端口，默认 `8000` |
| `DATABASE_PATH` | SQLite 路径，默认 `./data/personal_ai_os.db` |
| `RECOMMENDATIONS_USE_MODEL` | 设为 `1` 才允许建议引擎调用云端模型润色 |
| `REMINDER_SCHEDULER_ENABLED` | `1/true/yes/on` 启用自动提醒 tick；默认关闭 |
| `REMINDER_SCHEDULER_POLL_SECONDS` | 自动 tick 轮询间隔，默认 60，配置下限 30 秒 |
| `REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | 关闭时等待当前同步 tick 的有界时间，默认 35 秒 |
| `PAOS_SECRET_KEY` / `PAOS_SECRET_KEY_PATH` | connector 凭据加密 key；日常建议使用默认本地 key 文件 |

完整说明见 [docs/configuration.md](docs/configuration.md)。不要把真实密钥写入 README、测试、前端、`run.bat` 或计划文档。

## 6. 启动

### Windows 推荐方式

```powershell
.\run.bat
```

脚本会：

1. 切换到脚本所在项目根目录；
2. 检查 `.venv\Scripts\python.exe`；
3. 在外部环境未定义变量时加载 `.env`；
4. 默认监听 `http://127.0.0.1:8000`，且只允许 loopback Host；
5. 检查端口是否已被监听；
6. 透传 Uvicorn 退出码。

脚本不会自动安装依赖或重置数据库，也不会额外执行独立 tick；应用内自动提醒调度仅在 `REMINDER_SCHEDULER_ENABLED=1` 时由 lifespan 启动。

### 手动启动

普通 PowerShell 会话不会自动读取 `.env`，需要先设置环境变量，或只使用本地演示模式：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发时可加 `--reload`。多 worker/热重载场景下不要依赖进程内锁做跨进程互斥。

## 7. 测试

静态编译：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`tests/conftest.py` 会强制测试使用系统 Temp 下的 SQLite 数据库和临时密钥路径，并清空继承的云端 Key。外部 HTTP 请求通过 mock transport 或 DNS 打桩处理；跨进程租约测试使用真实 OS 子进程和临时数据库。

C-C-2c 本轮最终验证结果：`445 passed, 1 skipped, 1 warning`。唯一跳过项是当前环境不能创建符号链接；警告为 Starlette TestClient/httpx 兼容性提示。详细记录见 [docs/testing.md](docs/testing.md)。

2026-10-21 缺陷修复批次（设置接口 / 错误路径 / 租约时钟 / snooze / 日期炸弹 / 并发夹具）后的当前结果：`488 passed, 1 skipped, 1 warning`，连续两次全量运行一致。历史结果（`445`、`412`）不得与当前结果混用。

## 8. 数据库安全

默认真实开发数据库：

```text
data/personal_ai_os.db
```

注意：

- 不要删除、覆盖、重建或迁移真实库来“验证测试”。
- 测试必须使用临时数据库。
- 应用 startup 会执行现有 `init_db()` 幂等建表/迁移；在生产或个人真实库上启动前建议先备份。
- connector 凭据加密 key 默认位于 `data/secret.key`；复制数据库时要同时考虑 key 的安全保管，否则凭据无法解密。
- `init_db()` 会执行 `PRAGMA journal_mode = WAL`，因此**应用启动后真实库即处于 WAL 模式**（`synchronous=NORMAL`），启动日志会打印实际模式。WAL 下的备份必须先停应用，再复制 `personal_ai_os.db` 与 `personal_ai_os.db-wal`、`personal_ai_os.db-shm`；只复制 `.db` 会丢掉尚未 checkpoint 的事务。需要在运行中取一致快照时用 `VACUUM INTO 'backup.db'`。

详见 [docs/database.md](docs/database.md)。

## 9. 常用 API

- 健康检查：`GET /api/health`
- 档案：`GET/PUT /api/profile`
- 记忆：`/api/memories`、`/api/memory-candidates`、`/api/memory-insights`
- 文档：`/api/documents`、`/api/documents/upload`、`/api/documents/text`
- 项目/目标/任务：`/api/projects`、`/api/goals`、`/api/tasks`（含 `PATCH`/`DELETE` 与 `/api/projects/{id}/events`，Web UI 已全部接入）
- 画像：`/api/profile-items`
- 连接器：`/api/connector-types`、`/api/data-sources`
- 行为分析：`/api/behavior/*`、`/api/analytics/*`
- 建议：`/api/recommendations`、`/api/recommendation-runs`、feedback/outcome 子资源
- 对话：`/api/chat`、`/api/conversations`
- 审批：`/api/approvals`

API 响应和错误码以源码路由及测试为准。

## 10. 文档索引

- [架构说明](docs/architecture.md)
- [模块职责](docs/module-responsibilities.md)
- [数据库说明](docs/database.md)
- [配置说明](docs/configuration.md)
- [开发工作流](docs/development-workflow.md)
- [当前任务](docs/current-task.md)
- [项目进度](docs/progress.md)
- [技术决策](docs/decisions.md)
- [测试记录](docs/testing.md)
- [阶段计划](docs/plans/README.md)

## 11. 常见问题

### 不配置 LLM API Key 可以运行吗？

可以。聊天使用本地演示逻辑；依赖云端模型的记忆提取和画像推测会返回相应的空结果或 400。

### 可以绑定 `0.0.0.0` 或公网吗？

不可以直接这样部署。当前没有登录和多用户权限体系，只应在 loopback 使用。

### 为什么提醒没有自动弹出？

自动提醒 tick 已接入应用生命周期但默认关闭。当前没有邮件、短信、Webhook 或桌面通知发送器；tick 只在 SQLite 中创建/推进提醒状态。自动调度采用 at-least-once 片段提交语义，不是 exactly-once。

### 测试会写入我的真实数据库吗？

正常不会。`tests/conftest.py` 在 pytest 启动阶段强制重定向到系统 Temp 数据库；不要设置 `PAOS_TEST_ALLOW_REAL_DATABASE=1`。

### 为什么建议功能没有调用模型？

建议引擎默认只使用确定性规则。只有 `LLM_API_KEY` 存在且 `RECOMMENDATIONS_USE_MODEL=1` 时，模型才能润色程序已生成的建议。

## 12. 下一阶段建议

如需启用自动 tick，仅在本地 loopback 环境设置：

```powershell
$env:REMINDER_SCHEDULER_ENABLED="1"
$env:REMINDER_SCHEDULER_POLL_SECONDS="60"
$env:REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS="35"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

多 worker/热重载时各进程都可启动循环，但只有持有 SQLite lease 的进程执行 tick；busy 是正常结果。shutdown 不会强杀同步 SQLite 线程，超时后依赖 30 秒 lease TTL、fencing 和后续 recovery。设计见 [C-C-2c 设计文档](docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md)。
