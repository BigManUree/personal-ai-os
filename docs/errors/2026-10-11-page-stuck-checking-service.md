# 2026-10-11 页面一直「正在检查服务」且所有按钮无响应

## 错误现象
浏览器打开 http://127.0.0.1:8000/ 后，右上角状态一直停留在「正在检查服务…」，页面任何按钮都点不动。

## 发生场景
阶段 3.4 前端改造：在 `app/static/index.html` 新增「项目与任务」页并追加 `work.js` 的 `<script src>` 之后。后端 `/api/health` 实际正常返回 `{"status":"ok","mode":"cloud"}`，curl 访问 200，问题仅在前端。

## 根因分析
统计标签发现页面有 4 个 `<script ...>` 开标签、只有 3 个 `</script>` 闭标签——内联脚本末尾（初始化 IIFE 之后、外部脚本之前）漏掉了一个 `</script>`。浏览器因此把后面的 `<script src=...>` 当作内联脚本文本解析，整个内联 `<script>` 抛语法错误：
- `checkHealth()` 从未执行 → 状态永久「正在检查服务」；
- 所有 `addEventListener`（导航、表单、按钮）都没注册 → 页面看起来「点不动」。

## 解决方案
在初始化语句 `(async()=>{...})();` 之后、`<script src="/static/memory-candidates.js">` 之前补回缺失的 `</script>`。

## 验证
- 修复后开/闭标签数均为 4；HTTP 实际下发页面的内联脚本 `node --check` 通过，`work.js` 通过。
- 全量 `pytest -q` 60 项通过（该问题纯前端，不影响后端测试）。
- 用户硬刷新（Ctrl+F5）后恢复。

## 经验
今后改动 index.html 后，应把内联脚本抽出做一次 `node --check`，并核对 `<script>` 开闭标签数量，避免同类静默失效。

---

## 复发与根治（同日晚，1.3.1 开工前）

### 复发现象
`</script>` 早已补齐、磁盘文件与服务实际下发的页面都正确（Playwright 无头浏览器全新加载：状态显示「云端模型已配置」、导航可点、无 console 报错），但用户浏览器仍一直「正在检查服务」。

### 根因
浏览器命中了**修复前坏页面的缓存**。首页路由 `FileResponse(index.html)` 没有下发任何缓存控制头，响应只有 `Last-Modified/ETag`；某些情况下标签页继续复用已缓存的旧 HTML（其中内联脚本仍是坏的），于是现象与最初一致。

### 根治
给首页显式加上禁止缓存头（`app/main.py` 的 `index()`）：

```python
return FileResponse(
    STATIC_DIR / "index.html",
    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
)
```

并用项目 venv 重启服务（直接 uvicorn 启动不会加载 `.env`，需先注入 `LLM_*` 环境变量，否则健康检查会退回 local-demo）。重启后响应头确认含 `Cache-Control: no-store, must-revalidate, no-cache`，Playwright 复验通过，`pytest -q` 不受影响。

### 经验
- 本地开发期 HTML 入口应默认 `no-cache`，静态 JS 可保留缓存；否则前端修复容易被旧缓存「假性复现」。
- 排查「脚本整体失效」类问题时，优先用全新自动化浏览器（无缓存、无扩展）复现，可快速区分代码问题与客户端缓存问题。
