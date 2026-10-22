# Implementation Plan: 建议采纳与生成入口

**Feature**: `002-recommendation-actions` · **Spec**: [spec.md](./spec.md) · **Date**: 2026-10-22

## 范围

单文件前端改造 + 一处测试接线，不改后端契约。

| 文件 | 变更 |
|---|---|
| `frontend/src/pages/Recommendations.vue` | 重写：状态标签、动作按钮、拒绝二次确认、生成面板、反馈并行加载 |
| `tests/test_recommendations_api.py` | 前端接线断言迁移到构建产物 + 组件源码 |
| `app/static/**` | `npm run build` 重新生成（`emptyOutDir: true`） |

## 接口契约（已存在，不修改）

| 动作 | 路由 | 前置状态 → 目标状态 |
|---|---|---|
| accept | `POST /api/recommendations/{id}/accept` | pending → accepted |
| ignore | `POST /api/recommendations/{id}/ignore` | pending → ignored |
| reject | `POST /api/recommendations/{id}/reject` | pending → rejected（30 天冷却） |
| reopen | `POST /api/recommendations/{id}/reopen` | accepted → pending |
| generate | `POST /api/recommendations/generate` | body `{days: 7\|14\|30, use_model: bool}` |

列表：`GET /api/recommendations?status=` —— 不传 `status` 时服务端隐藏 `rejected/expired` 且只返回未过期项；传 `pending|accepted` 时附带过期过滤；传终态时不过滤。

## 关键取舍

1. **动作后局部更新 + 按需刷新**：`accept` 后把响应对象合并回当前行，避免整表重刷造成滚动跳动；仅当该条不再属于当前筛选（如默认视图下变成 ignored）才整表刷新。
2. **拒绝用内联二次确认**，不引入全局 modal 组件：当前 `frontend/src/components/` 为空，为一个确认框新建组件层不划算。
3. **反馈并行拉取**：`Promise.all` 替代串行 `for await`；失败按 `null` 处理，不阻塞列表渲染。
4. **生成面板不自动收起**：结果摘要（状态/窗口/缺口）留在面板内，收起就看不到。

## 验证方式

```powershell
cd frontend; npm run build; cd ..
.\.venv\Scripts\python.exe -m pytest -q
# 真实浏览器冒烟：临时库 + 127.0.0.1:18766 + Playwright
node <e2e script> http://127.0.0.1:18766
```

浏览器冒烟使用临时数据库（`DATABASE_PATH` 指向 `temp/ui-smoke.db`），未触碰 `data/personal_ai_os.db`。

## 风险与未覆盖

- **未覆盖**：outcome 面板、统计面板、建议详情页、分页（当前 limit 固定 50）。
- 生成接口同步执行；有大量行为事件时请求耗时取决于规则计算，本轮未做超时/取消。
- 浏览器冒烟脚本是一次性验证脚本，未纳入仓库（`frontend/e2e_regression.mjs` 仍是既有的全量脚本，端口 18765）。
