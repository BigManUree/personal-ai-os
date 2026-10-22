# 错误记录：删除接口测试收到 422 而非 404（测试字符串漏写 f 前缀）

- 日期：2026-10-11
- 阶段：阶段 2 / 迭代 01–02（长期记忆引擎）
- 严重程度：低（仅测试代码问题，非产品代码缺陷）

## 现象

运行 `pytest` 时 `test_legacy_memory_api_still_works` 失败：

```
AssertionError: assert 422 == 404
E        +  where 422 = <Response [422 Unprocessable Entity]>.status_code
E        +    and   422 = client.delete('/api/memories/{mid}')
```

已删除的记忆再次删除，期望 `404 记忆不存在`，实际返回 422。

## 原因

测试最后一行写成了普通字符串（缺少 `f` 前缀）：

```python
assert client.delete("/api/memories/{mid}").status_code == 404
```

请求路径里的 `{mid}` 没有被插值，字面值 `{mid}` 无法转换为整数路径参数，FastAPI/Pydantic 在路由参数校验阶段直接返回 422，请求根本没有进入处理函数，因此不会走到 404 分支。产品代码 `DELETE /api/memories/{id}` 行为本身正确（同用例前半段删除返回 `{"deleted": true}` 已验证）。

## 处理

补上 f 前缀：

```python
assert client.delete(f"/api/memories/{mid}").status_code == 404
```

## 教训 / 预防

- 路径含变量插值时检查是否为 f-string；422（参数校验失败）与 404（资源不存在）要区分定位：先确认请求是否真正进入处理函数。
- 涉及路径参数的接口测试，应同时覆盖「合法但不存在的 id（→404）」与「非法格式 id（→422）」两种情况。
