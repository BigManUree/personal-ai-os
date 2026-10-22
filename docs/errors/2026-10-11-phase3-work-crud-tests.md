# 2026-10-11 阶段 3.4 首轮测试三处失败

> 场景：为项目/目标/任务模块新增 `tests/test_work.py` 后首次运行，9 个用例中 3 个失败。均为真实实现缺陷（非测试写法问题），已修复。

## 错误 1：空白标题未被拒绝（期望 422，实际 200）

- 现象：`POST /api/projects` 传 `{"name": "   "}` 返回 200。
- 原因：Pydantic 的 `Field(min_length=1)` 只校验字符数，纯空白字符串能通过；端点只 `.strip()` 不判空。
- 修复：新增共享校验 `_non_blank`，用 `@field_validator` 对 ProjectIn.name / ProjectEventIn.content / GoalIn.title / TaskIn.title 统一拒绝空白（Pydantic 2.x，项目用 2.14.0）。

## 错误 2：创建任务返回缺 project_name（KeyError）

- 现象：`POST /api/tasks`（带 project_id）后读 `t["project_name"]` 抛 KeyError；列表接口有 join，创建接口只查 tasks 单表。
- 修复：创建接口改为 `tasks t LEFT JOIN projects p` 查询返回，与列表接口字段一致。

## 错误 3：注入隔离测试的 httpx mock 签名不匹配（TypeError → 502）

- 现象：`call_llm` 重试 5 次后抛 502，DummyClient.post 收到未声明的 `headers` 关键字。
- 原因：真实调用为 `client.post(url, headers=..., json=...)`，测试替身漏了 `headers` 形参。
- 修复：mock 方法签名改为 `async def post(self, url, json, headers=None)`；确认这是测试替身问题，生产重试逻辑正常。

## 验证

修复后 `tests/test_work.py` 9 项通过，全量 `pytest -q` 60 项通过。
