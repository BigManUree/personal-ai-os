# 开发工作流

更新时间：2026-10-13 18:27 CST

## 1. 开始新任务前

1. 阅读 `AGENTS.md` 的安全红线。
2. 查看 `docs/current-task.md`，确认当前状态和唯一下一步。
3. 查看相关代码与测试，不把历史计划当作已实现功能。
4. 若目录是 Git 仓库，先查看工作区状态；本项目当前核验时不是 Git 仓库，不能自动 `git init`。
5. 涉及数据库时先确认真实库路径；默认真实库为 `data/personal_ai_os.db`。

## 2. 环境准备

当前开发环境：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

本机本轮核验解释器：Python 3.13.9。项目声明兼容 Python 3.11+。

## 3. 启动应用

推荐使用：

```powershell
.\run.bat
```

或手动启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`run.bat` 只在变量未由外部环境设置时读取 `.env`，不自动安装依赖、不执行数据库迁移脚本、不启动提醒 tick。应用 startup 中的 `init_db()` 是现有 FastAPI 生命周期行为。

## 4. 修改代码

- 小步修改；一个提交/工作单元只处理一个明确主题。
- 优先保持公开 API、配置键、数据库字段和前端调用兼容。
- 不为形式创建空的 `api/`、`services/`、`repositories/`、`utils/` 目录。
- 需要移动文件时，同步修复导入、测试、文档链接、启动脚本和前端路径。
- 修改调度器、提醒、数据库事务、凭据或连接器前，先阅读对应测试。
- 领域模块不得反向导入 `app.main`。
- 外部输入、文档内容、模型响应均按不可信数据处理。

## 5. 数据库安全流程

允许：

- 使用 `mode=ro` URI 只读查看真实库；
- 对真实库计算大小、mtime、hash；
- 在系统 Temp 目录创建独立 SQLite 数据库；
- 运行已有隔离测试。

禁止（除非用户明确授权）：

- 删除、覆盖、重建或迁移 `data/personal_ai_os.db`；
- 对真实库执行 seed、修复、初始化、`run_tick()`；
- 在真实库上测试 connector credential 加解密；
- 复制真实库到项目内临时位置后误当测试库使用。

新增数据库测试必须同时设置：

- `DATABASE_PATH` 到系统 Temp；
- `PAOS_SECRET_KEY_PATH` 到系统 Temp（全局 conftest 已默认处理）；
- monkeypatch 已导入的 `app.main.DB_PATH`。

## 6. 测试流程

最低静态检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

相关模块测试示例：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_model_config.py tests/test_api.py -q
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

要求：

- 不删除失败测试或放宽关键断言；
- 不依赖真实网络、真实 LLM、真实通知或真实开发库；
- 显式时间优先于系统当前时间；
- 测试结束后复核真实开发库 hash/mtime；
- 历史测试结果必须注明时间，本轮结果写入 `docs/testing.md`。

## 7. 调度器任务流程

1. 先阅读 `docs/plans/phase-5-1.3.5-reminders-c-c-2c-design.md`。
2. 自动提醒生命周期已经接入 lifespan，但默认关闭；修改前确认默认配置和测试环境均不会启动指向真实库的循环。
3. 继续实现或加固时必须区分：
   - asyncio 控制循环；
   - 专用执行器中的同步 SQLite `run_tick()`；
   - SQLite lease/fencing；
   - heartbeat/续租；
   - shutdown 等待/超时；
   - recovery 和观测。
4. 多 worker/热重载只能依赖 SQLite 租约做跨进程互斥。
5. 取消 asyncio wrapper/Future 不等于停止同步 SQLite worker；必须保留有界 shutdown、TTL、fencing、recovery 测试。
6. 不得把当前系统描述为 exactly-once，也不得伪装存在真实通知发送。
7. 修改后运行生命周期测试、提醒专项和全量测试。

## 8. 代码审查清单

- API 路径、请求体和响应字段是否保持兼容；
- 写操作是否在明确事务中，异常是否回滚；
- 是否误把用户文档/模型输出当指令；
- 是否有硬编码密钥、绝对路径或公网监听；
- 是否新增未被多个模块使用的“公共工具”；
- 是否有循环导入或领域模块依赖 FastAPI；
- 测试是否隔离数据库、时间、网络和密钥；
- 文档是否区分已实现、设计中、未开始。

## 9. 更新交接文档

任务结束时更新：

- `docs/current-task.md`：本轮完成、风险、唯一下一步；
- `docs/progress.md`：各事项状态；
- `docs/decisions.md`：只记录本批实际确认的决策；
- `docs/testing.md`：本轮真实命令和结果；
- 必要时更新 `README.md`、`docs/architecture.md`、`docs/database.md`、`docs/configuration.md`。

## 10. 新 Codex 会话恢复

新会话优先阅读：

1. `AGENTS.md`
2. `docs/current-task.md`
3. `docs/progress.md`
4. 相关 `docs/plans/*.md`
5. 对应源码和测试

如果状态文档与源码冲突，以源码和本轮可重复测试为准，再修正文档。


## 11. 通知投递任务流程

1. 修改前阅读 `docs/plans/phase-5-1.3.6-notification-delivery-design.md`、`app/notification_delivery.py` 和 `tests/test_notification_delivery.py`。
2. 通知 event/delivery 必须由提醒 due 事务持久化；不要先提交提醒状态再依赖内存队列。
3. 默认关闭 `NOTIFICATION_DELIVERY_ENABLED`；测试中显式启用时也只能使用 `fake_test`。
4. 多进程领取以 SQLite 条件 UPDATE 为准；不要用 asyncio/threading Lock 替代数据库协议。
5. 错误信息必须通过脱敏函数，避免 token、API key、Authorization header 或完整敏感负载入库。
6. 修改后运行通知专项、提醒专项、编译检查和全量 pytest；真实库只允许只读 hash/pragma 审计。
