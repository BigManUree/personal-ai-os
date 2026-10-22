# 配置说明

更新时间：2026-10-13 18:27 CST

## 1. 配置加载方式

- 普通 Uvicorn/Python 进程不会自动解析 `.env`。
- `run.bat` 会在变量未由外部环境定义时，从项目根目录的 `.env` 逐行加载简单 `KEY=VALUE` 配置。
- 应用代码通过 `os.getenv()` 在运行时读取环境变量；云端模型默认值和解析集中在 `app/model_config.py`。
- `.env.example` 只保存变量名和非敏感示例，不保存真实密钥。

## 2. 变量清单

| 变量 | 必填 | 默认值 | 作用 | 主要使用位置 | 缺失/异常行为 |
|---|---:|---|---|---|---|
| `LLM_API_KEY` | 否 | 空字符串 | OpenAI 兼容接口 API Key；为空时聊天使用本地演示逻辑 | `app/model_config.py`，由聊天、记忆提取、画像推测、建议模型调用 | 为空不报错；记忆提取/画像推测等云端旁路返回 400 或空结果，具体由 API 决定 |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容 Chat Completions API 基础 URL | `app/model_config.py` | 使用默认 OpenAI 地址；尾部 `/` 会被移除 |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 聊天、记忆提取、画像和建议润色使用的模型名 | `app/model_config.py` | 使用默认模型；模型不存在或鉴权失败由 httpx/接口返回错误路径处理 |
| `APP_HOST` | 否 | `127.0.0.1`（run.bat） | `run.bat` 启动 Uvicorn 的监听地址 | `run.bat` | 只允许 `127.0.0.1`、`localhost`、`::1`；非 loopback 值会被启动脚本拒绝 |
| `APP_PORT` | 否 | `8000`（run.bat） | `run.bat` 启动 Uvicorn 的端口 | `run.bat` | 未设置时使用 8000；端口已监听时脚本退出码 1 |
| `DATABASE_PATH` | 否 | `<项目根>/data/personal_ai_os.db` | SQLite 数据库文件路径 | `app/main.py` | 模块导入时确定默认路径；父目录会被创建。测试必须改到临时目录 |
| `RECOMMENDATIONS_USE_MODEL` | 否 | 空/非 `1` 即关闭 | 建议引擎是否允许调用云端模型润色规则建议 | `app/recommendation_engine.py`、`app/main.py` | 即使配置了 `LLM_API_KEY`，未显式设为 `1` 也只使用确定性规则 |
| `REMINDER_SCHEDULER_ENABLED` | 否 | 未设置/`0`/false/no/off 即关闭 | 是否启动自动提醒 tick 循环 | `app/reminder_scheduler_service.py`、`app/main.py` | 默认不启动；`1/true/yes/on` 启用；非法值在 `init_db()` 前由 lifespan 校验并终止启动 |
| `REMINDER_SCHEDULER_POLL_SECONDS` | 否 | `60` | 自动 tick 基础间隔，同时决定 scheduled run key 时间桶 | `ReminderSchedulerService` | 环境配置允许 `30..3600` 秒；测试可注入更小值；非法值启动时报错 |
| `REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | 否 | `35` | 关闭时等待当前同步 tick 的有界时间 | `ReminderSchedulerService.stop()` | 允许 `1..300` 秒；超时不强杀线程，状态记录为 `stopped_with_active_tick` |
| `PAOS_SECRET_KEY` | 否 | 空 | urlsafe base64 编码的 32 字节 Fernet key，可替代本地 key 文件 | `app/secret_crypto.py` | 不建议日常使用；值非法时凭据加解密报 `SecretCryptoError` |
| `PAOS_SECRET_KEY_PATH` | 否 | `<项目根>/data/secret.key` | Fernet key 文件路径 | `app/secret_crypto.py` | 文件不存在且需要加解密时会生成新文件；测试必须指向临时路径 |
| `PAOS_TEST_ALLOW_REAL_DATABASE` | 否 | 非 `1` 即拒绝 | 仅用于人工授权测试连接真实数据库 | `tests/conftest.py` | 正常测试必须保持未设置；设为 `1` 会关闭防误写守卫 |

## 3. 推荐本地配置

`.env.example`：

```text
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
APP_HOST=127.0.0.1
APP_PORT=8000
DATABASE_PATH=./data/personal_ai_os.db
RECOMMENDATIONS_USE_MODEL=0
REMINDER_SCHEDULER_ENABLED=0
REMINDER_SCHEDULER_POLL_SECONDS=60
REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS=35
PAOS_SECRET_KEY=
PAOS_SECRET_KEY_PATH=./data/secret.key
```

PowerShell 中也可以只在当前会话设置云端模型：

```powershell
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_API_KEY="你的 API Key"
$env:LLM_MODEL="gpt-4o-mini"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 4. 密钥处理规则

- 不把真实 Key 写入 `run.bat`、README、文档、测试、前端或计划文档。
- connector 配置中的明文 token 会被检测并拒绝；需要凭据时使用专门的 credential API 和 `connector_credentials` 密文表。
- 日志和错误信息应依赖现有 `redact_secrets()` / `sanitize_error()` 路径，不自行拼接可能含密钥的配置。
- `.gitignore` 忽略 `.env`、`.env.*`（保留 `!.env.example`）、`data/secret.key`、数据库和备份文件。

## 5. 测试环境

`tests/conftest.py` 会在 pytest 启动时：

1. 将 `DATABASE_PATH` 改到系统 Temp 下的独立 pytest 目录；
2. 将 `PAOS_SECRET_KEY_PATH` 改到同一临时目录；
3. 删除继承的 `LLM_API_KEY` 和 `PAOS_SECRET_KEY`；
4. 每个测试自动确认当前环境仍指向临时库。

需要模拟云端 Key 的测试应自行设置假值并 mock HTTP，不得访问真实网络。


## 6. 自动提醒调度配置说明

自动调度只适合本机 loopback 单用户场景。启用后每个 Uvicorn worker/热重载进程都可能启动循环，但只有持有 SQLite 全局 lease 的连接执行 `run_tick()`；其他进程得到 busy 并等待下一周期。

启用示例：

```powershell
$env:REMINDER_SCHEDULER_ENABLED="1"
$env:REMINDER_SCHEDULER_POLL_SECONDS="60"
$env:REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS="35"
```

启用不会配置通知发送器，也不会访问外部通知 API。自动 tick 采用 at-least-once 片段提交语义，不应把它描述为 exactly-once。


## 7. 通知投递配置（C-C-2d）

| 变量 | 是否必填 | 默认值 | 作用 | 使用模块 | 缺失/非法行为 |
|---|---:|---|---|---|---|
| `NOTIFICATION_DELIVERY_ENABLED` | 否 | 未设置/`0/false/no/off` 即关闭 | 是否启动通知消费循环 | `app/notification_delivery_service.py`、`app/main.py` | 默认只持久化 pending delivery，不消费；非法值在 `init_db()` 前终止 lifespan |
| `NOTIFICATION_DELIVERY_POLL_SECONDS` | 否 | `30` | 消费循环空转后的轮询间隔 | `NotificationDeliveryService` | 环境配置允许 `5..3600` 秒；测试可注入更小值 |
| `NOTIFICATION_DELIVERY_SHUTDOWN_TIMEOUT_SECONDS` | 否 | `35` | 关闭时等待当前投递的最长时间 | `NotificationDeliveryService.stop()` | 允许 `1..300` 秒；超时不强杀线程，状态暴露为 `stopped_with_active_delivery` |

即使启用，当前版本也只接受 `fake_test` 渠道；这些变量不会启用真实邮件、短信、Webhook、桌面通知或云服务。
