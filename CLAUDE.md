---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 80db606b7013a01cbccf2a0b51c86f97_e8f54a30c7d811f19268525400cd780f
    ReservedCode1: xbKdniQxElZozfW3z7Qro8sQGiMUxfh/IHI+aroThXhxExcDrVQ/MAWw6T2kUws9UmrgqDeMVz7cNCLHkQQcuj0EhK3egRpdBlnBdaP+i9f/DpvKCCUXoai84TrstewUjMcnie09U5iQXj1eLAPZ4f9SCP6hB6TwX6iOs4LzwCAcMZFtBBOQjtXX2F8=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 80db606b7013a01cbccf2a0b51c86f97_e8f54a30c7d811f19268525400cd780f
    ReservedCode2: xbKdniQxElZozfW3z7Qro8sQGiMUxfh/IHI+aroThXhxExcDrVQ/MAWw6T2kUws9UmrgqDeMVz7cNCLHkQQcuj0EhK3egRpdBlnBdaP+i9f/DpvKCCUXoai84TrstewUjMcnie09U5iQXj1eLAPZ4f9SCP6hB6TwX6iOs4LzwCAcMZFtBBOQjtXX2F8=
---

# CLAUDE.md — Personal AI OS MVP

项目协作规则以 `AGENTS.md` 为准，开始工作前请先完整阅读。核心约束摘要：

- 本地优先 FastAPI + SQLite 单用户应用，前端为 `app/static/` 原生 HTML/CSS/JS，无构建步骤。
- Python 3.11+，解释器 `.venv/Scripts/python.exe`；依赖：`-m pip install -r requirements.txt`（测试另加 `requirements-dev.txt`）。
- 应用入口 `app.main:app`；修改 `app/main.py` 必须控制范围；领域模块不得反向导入 FastAPI。
- 安全红线：不触碰 `data/personal_ai_os.db` 与密钥；所有数据库测试使用系统临时目录；`REMINDER_SCHEDULER_ENABLED` / `NOTIFICATION_DELIVERY_ENABLED` 默认保持 `0`，不引入真实通知渠道；默认只绑定 loopback。
- 验证：`.venv/Scripts/python.exe -m compileall -q app tests` 与全量 pytest 必须通过。

## Spec Kit 工作流（强制）

本项目已接入 speckit（技能在 `.claude/skills/speckit-*`，基础设施在 `.specify/`）。任何功能性代码变更（新功能、需求变更、行为/接口/数据模型相关修复）**必须**依次执行，不得直接改代码：

1. `/speckit-specify` — 生成或更新规格（自动创建 `specs/<编号>-<名称>/` 并登记 `.specify/feature.json`）
2. `/speckit-plan` — 生成实现方案
3. `/speckit-tasks` — 拆任务清单 `tasks.md`
4. `/speckit-implement` — 按任务实现并逐项验证
5. `/speckit-converge` — 收尾核查缺口并追加任务

规则要点：

- 开始前先看 `.specify/feature.json` 定位当前特性目录；修改既有特性先更新 `spec.md` 再走后续流程。
- 新特性从 `/speckit-specify` 开始。
- 豁免（提交说明标注 `skipped speckit: trivial`）：纯文档更新、格式整理、不影响行为/接口的测试断言修复、依赖版本升级。
- 一键自动化：`specify workflow run "<需求描述>"`（内置 Full SDD Cycle，带人工 review gate）。
*（内容由AI生成，仅供参考）*
