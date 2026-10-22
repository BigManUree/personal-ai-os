# AGENTS.md — Personal AI OS MVP 协作规则

更新时间：2026-10-13 20:24 CST

## 项目概况

- 本地优先的 FastAPI + SQLite 单用户应用，前端为 `frontend/` 下的 Vue 3 + Vite 工程，构建产物输出到 `app/static/`。
- Python 3.11+，当前开发虚拟环境解释器为 `.venv/Scripts/python.exe`（本机实测 Python 3.13.9）。
- 依赖安装：`.venv/Scripts/python.exe -m pip install -r requirements.txt`；测试另需 `requirements-dev.txt` 中的 pytest。
- 应用入口：`app.main:app`；Windows 启动脚本：`run.bat`；手动启动：`.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。

## 目录与依赖边界

- `app/main.py`：FastAPI 组装、HTTP 路由、SQLite schema 初始化/增量迁移、应用生命周期；文件较大，修改时必须控制范围。
- `app/*_store/domain/service 模块`：当前项目采用按领域拆分的扁平模块，而不是强制 Repository/Service 目录；不要为了形式新增空层。
- `app/reminders.py`：提醒设置、提醒存储、规则与状态机；不得加入 HTTP 路由或后台循环。
- `app/reminder_scheduler.py`：确定性的同步调度核心、长 tick 边界续租/停止检查与 fencing 写入保护；不得在其中直接启动 asyncio 循环或接入 FastAPI 生命周期。
- `app/reminder_scheduler_service.py`：opt-in asyncio 控制循环和专用 daemon 单线程执行器；只负责定时、状态观测和关闭，不拥有跨进程互斥权威。
- `app/reminder_scheduler_lock.py`：SQLite 跨进程租约；是多进程互斥权威，进程内 Lock 不能替代它。
- `app/notification_delivery.py`：提醒到期通知事件、投递记录、幂等键、claim/重试/死信/恢复和 `fake_test` 测试渠道；不得加入真实外部渠道。
- `app/notification_delivery_service.py`：默认关闭的 asyncio + 单线程 fake 消费服务；跨进程领取权威是 SQLite 条件 UPDATE，不是进程内 Lock。
- `app/connector_scheduler.py`：仅数据源同步的 opt-in asyncio 循环；其进程内锁不是跨进程锁。
- `app/static/`：Vite 构建产物（`index.html` + `assets/index-<hash>.js|css`），由 `frontend/` 生成，不手改、不保存密钥。
- `frontend/src/`：Vue 3 源码（`App.vue`、`pages/*.vue`、`api.ts`）；修改后必须 `npm run build` 才会生效。
- `tests/`：pytest 测试；所有数据库测试必须使用系统临时目录中的 SQLite 文件。
- `docs/plans/`：设计/阶段计划；设计文档存在不代表功能已实施。

依赖方向：HTTP/生命周期层可以调用领域模块；领域模块不得反向导入 `app.main` 或 FastAPI。共享配置放入明确的领域/基础模块（例如 `app/model_config.py`），不得把杂项代码堆入 `utils/`。

## 安全红线

- 不删除、覆盖、重建或迁移 `data/personal_ai_os.db` 及任何备份文件。
- 不在真实开发数据库上运行初始化、seed、修复脚本或测试写入；需要数据库时使用临时库。
- 不对真实开发库调用 `app.reminder_scheduler.run_tick()`、lease acquire/heartbeat/release 或通知 claim/delivery；临时库测试除外。
- `REMINDER_SCHEDULER_ENABLED` 默认必须保持 `0`；启用只表示自动执行本地 SQLite tick，不代表已有真实通知发送器。
- `NOTIFICATION_DELIVERY_ENABLED` 默认必须保持 `0`；即使显式启用，也只允许 `fake_test` 本地测试渠道，严禁加载邮件、短信、Webhook、桌面或云通知渠道。
- 取消 asyncio wrapper/Future 不等于同步 SQLite tick 已停止；关闭逻辑必须保留有界等待、lease TTL、fencing 和 recovery 语义。
- 不读取后输出 `.env`、`data/secret.key` 或任何密钥值；文档只记录变量名、默认值和非敏感示例。
- 不执行 `git reset --hard`、`git clean -fd`、强制推送；当前目录若不是 Git 仓库，不自动初始化。
- 默认只绑定 loopback；不得把无登录应用暴露到 `0.0.0.0` 或公网。

## 修改与验证要求

1. 修改前先读相关代码、测试和文档；不要只根据文件名或历史计划判断状态。
2. 小步修改，行为优先于目录形式；移动文件前必须修复导入、测试、启动脚本和文档链接。
3. 修改 Python 后至少运行相关测试；提交结论前运行：
   - `.venv/Scripts/python.exe -m compileall -q app tests`
   - `.venv/Scripts/python.exe -m pytest -q`
4. 测试使用 `tests/conftest.py` 的真实库保护；不要绕过该守卫，除非用户明确要求并设置 `PAOS_TEST_ALLOW_REAL_DATABASE=1`。
5. 修改提醒生命周期后至少运行 `tests/test_reminder_scheduler_lifecycle.py` 和提醒专项；修改通知后运行 `tests/test_notification_delivery.py`；提交结论前运行全量 pytest。
6. 外部网络必须通过 mock/httpx MockTransport；测试不得依赖真实 LLM、GitHub 或 HTTP API。
7. 不为通过测试而删除失败测试、放宽关键断言或把未实现功能标记为完成。

## 调度器特殊约束

- 自动提醒生命周期已由 lifespan 接入，但默认关闭；只有 `REMINDER_SCHEDULER_ENABLED=1/true/yes/on` 才启动循环。
- 修改前仍必须阅读 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md`；不得把自动 tick 描述成 exactly-once 或通知投递。
- 多 worker/热重载时，跨进程互斥以 SQLite lease/fencing 为准；asyncio Lock、threading Lock、单实例布尔值都只能防同进程重入。
- shutdown 超时、长 tick heartbeat、busy/lost/database_error、recovery 和观测字段必须配套测试。

## 文档交接

任务结束时至少核对并按需更新：

- `README.md`：真实启动、配置、测试、当前限制。
- `docs/current-task.md`：当前状态、唯一阻塞/下一步。
- `docs/progress.md`：已完成/进行中/未开始/阻塞。
- `docs/decisions.md`：本次实际做出的决策和否决理由。
- `docs/testing.md`：本轮实际命令、结果、跳过项和限制；历史结果必须标明时间。
- 架构或数据库变化时更新 `docs/architecture.md`、`docs/database.md`、`docs/module-responsibilities.md`。

遇到无法证明安全性的数据库、密钥、调度或外部通知操作，停止该操作，继续完成其他安全任务，并在交接文档中单列人工确认项。


## 通知投递特殊约束

- `scheduled/snoozed -> due` 与通知 event/delivery 创建必须在同一 SQLite guarded transaction 中完成；通知渠道失败不得回滚已完成的提醒状态。
- event/delivery 已持久化不等于送达；`succeeded` 仅表示 `fake_test` 测试替身返回成功。
- 多进程消费只能依赖数据库条件 UPDATE/行锁字段，不能假设 asyncio Lock 可跨进程。
- 不得提供任意 Python import、任意 URL webhook 或真实 channel loader；错误摘要必须脱敏。

## Spec Kit 工作流（强制）

本项目已接入 GitHub Spec Kit（speckit），技能位于 `.claude/skills/speckit-*`，基础设施在 `.specify/`。任何功能性代码变更（新功能、需求变更、行为/接口/数据模型相关 Bug 修复）**必须**依次执行完整工作流，不得直接修改代码：

1. `/speckit-specify` — 生成或更新规格文档（自动创建 `specs/<编号>-<名称>/` 目录并登记 `.specify/feature.json`）
2. `/speckit-plan` — 基于 spec 生成实现方案（plan/research/data-model/contracts/quickstart）
3. `/speckit-tasks` — 将方案拆为任务清单 `tasks.md`
4. `/speckit-implement` — 按任务实现，逐项勾选任务并跑验证
5. `/speckit-converge` — 收尾核查，对照 spec/tasks 检查缺口并追加任务

规则要点：

- 每次开发先看 `.specify/feature.json` 定位当前特性目录；修改既有已登记特性时先更新对应 `spec.md` 再走 plan→tasks→implement→converge。
- 新特性从 `/speckit-specify` 开始，自动编号新目录。
- 豁免（可跳过但必须在提交说明标注 `skipped speckit: trivial`）：纯文档更新、格式整理、不影响行为/接口的测试断言修复、依赖版本升级等 trivial 变更。
- 产物留在 `specs/` 与 `.specify/`，不得删除或另建平行体系；规格资产与 `docs/` 文档并存。
- 如需一键自动化：可用 `specify workflow run "<需求描述>"` 执行内置 Full SDD Cycle（specify→plan→tasks→implement，带人工 review gate）。
