# Implementation Plan: 建议结果登记与统计洞察

**Feature**: `004-recommendation-insights` · **Spec**: `specs/004-recommendation-insights/spec.md`

## 技术上下文

- 前端：Vue 3 `<script setup>`，`api()` 单入口，无状态库。
- 后端零改动（三组接口已存在并有 store 级测试）。
- 与 feature 003 的关键差异：**outcome 的 PUT 是部分更新**，工作项的 PATCH 是全量替换。两套语义并存，不能共用 body 映射思路。

## 组件设计

```
frontend/src/
├── components/
│   ├── OutcomeForm.vue                结果登记（accpeted 卡片内）
│   └── RecommendationInsights.vue     统计 + runs 历史（页面顶部折叠）
└── pages/
    └── Recommendations.vue            仅加 import、按钮、挂载点
```

### 为什么让组件自己加载

`Recommendations.vue` 的 `load()` 已经在并行拉 feedback（N 条建议 → N 个请求）。把 outcome 也塞进去会让它变成二维 Promise 矩阵，且 outcome 只在 `accepted` 时才需要。两个新组件各自 `onMounted` 加载，职责内聚，父级只负责"显示/隐藏"。

代价：每次展开统计面板都重新请求。单用户本地应用可以接受；不引入缓存层。

### OutcomeForm 状态机

```
初始: GET outcome
  ├─ null      → 编辑态，execution_status 未选，保存按钮可用但提交时拦截
  └─ 有值      → 编辑态回填 + 显示「删除结果」

保存: PUT 完整 4 字段
  ├─ 200 {status, outcome} → 回填，toast 显示 created/updated
  ├─ 409 → toast 服务端 detail + 保持表单内容（不丢弃用户输入）
  └─ 400 → toast detail

删除: confirm 内联 → DELETE → 清空表单 → 显示"未登记"
```

`execution_status` 从 `planned` 改为终态时服务端会自动补 `occurred_at`（`_outcome_payload`），前端不传该字段。

### 指标观察编辑器

- 行内 `metric` + `unit` + `expected_value` + `observed_value`（后两者 `type=number`，空字符串转为 `null`）。
- 保存前过滤：`metric` 或 `unit` 为空的行直接丢弃（服务端会因白名单校验 400，前端先拦）。
- 行数上限 10，达上限时「添加指标」禁用。
- `captured_at` 每次保存时重新取 `new Date().toISOString()`。

### 统计面板与比率

服务端 `_ratio()` 在分母为 0 时返回 `None`（`app/recommendation_stats.py:118`）。前端统一：

```ts
function fmtRatio(v: any): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—'
}
```

### 窗口参数

```
days → start_at = new Date(Date.now() - days*86400_000).toISOString()
       end_at   = new Date().toISOString()
```

服务端 `_parse_time` 接受带 `Z` 的 ISO 8601。

## 风险与取舍

| 风险 | 处理 |
| --- | --- |
| 把 outcome PUT 当全量替换 → 误以为省略字段会被清空 | spec 单独列"与上批相反"一节；测试断言前端不发送 `source`/`causality_status` |
| `metric_observations` 整体覆盖导致用户误删数据 | 表单始终展示并提交完整列表，删除行就是真删除，行为可预期 |
| 建议过期后仍能提交（前端没判断） | `expires_at < now` 时禁用保存并说明；服务端 409 兜底 |
| 统计面板把 `null` 显示成 0% | `fmtRatio` 单点实现，测试断言源码中存在 `'—'` 分支 |
| runs 的 `error` 含长文本撑破表格 | `slice(0, 200)` + `title` 原文 |
| 空库时统计面板报错 | 服务端返回 `quality.sample_status='no_data'` 与全 0 计数，前端正常渲染 |

## 验证计划

1. `tests/test_recommendation_insights_ui.py`：路由调用点（5 条）、枚举完整性、`fmtRatio` 空值分支、无危险 HTML。
2. `pytest -q` 全量。
3. `npm run build`。
4. Playwright（临时库 + 播种脚本）：
   - accepted 卡片出现登记入口，pending 不出现
   - 填 completed + better + 备注 + 1 条指标 → 保存 → API 校验 → 刷新回填
   - 删除结果 → API 返回 null
   - 统计面板：总数/状态分布/登记率与播种一致；`—` 出现在分母为 0 的比率
   - runs 表格：播种一次 generate 后出现一行
5. 真实库指纹校验。

## 回滚

- 前端新增两个组件 + 在 `Recommendations.vue` 加 3 处（import、按钮、挂载）。备份原文件到 `temp/bak-recommendations-vue-002.vue`。
- 回滚：还原该文件、删除两个组件与测试文件、`npm run build`。
- 后端零改动，无需回滚。
