# 2026-10-11 连接器配置密钥校验异常未转换导致创建数据源 500

## 错误现象
3.5.1 单跑 `tests/test_connectors.py` 时 14 过 1 失败：

```
FAILED tests/test_connectors.py::test_data_source_crud_and_validation
app.connectors.ConnectorConfigError: 连接配置中疑似包含密钥/令牌……
```

用例本意是「config 含 token 时返回 400」，实际异常直接冒泡成 500。

## 发生场景
`POST /api/data-sources` / `PATCH /api/data-sources/{id}` 中直接调用：

```python
connectors.assert_config_has_no_secrets(body.config)
```

该函数在命中密钥特征时抛的是连接器域异常 `ConnectorConfigError`（`app/connectors.py`，自 `Exception` 派生，**不是** `HTTPException`）。路由层没有捕获/转换，FastAPI 把未处理异常当 500。

单测里其余路径都走 HTTPException，唯独这个来自独立模块的自定义域异常没有边界转换，所以容易漏。

## 根因分析
分层边界没守住：独立模块（connectors.py）不应依赖 HTTP 层、因此抛域异常是对的；但 **HTTP 路由有责任把域异常翻译成 HTTP 状态码**。我在接入时漏掉了这一层 try/except。

## 解决方案及验证
在 create / patch 两个路由内包一层：

```python
try:
    connectors.assert_config_has_no_secrets(body.config)
except connectors.ConnectorConfigError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
```

验证：`tests/test_connectors.py` → **15 passed**；全量 `pytest -q` → **89 passed**。

## 预防
- 独立模块定义的自定义异常，跨到 HTTP 边界时必须有一处显式映射表（域异常 → HTTPException），review 新增路由时对照检查每个被调用的外部函数会抛哪些异常。
- 同步接口对 fetch 阶段的异常已统一在 sync 路由内分类处理（Auth/Fetch），配置阶段也应同等对待。
