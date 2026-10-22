# Implementation Plan: 个人画像条目管理

**Feature**: `005-profile-items` · **Spec**: `specs/005-profile-items/spec.md`

## 技术上下文

- 前端：Vue 3 `<script setup>`，`api()` 单入口。
- 后端零改动；`Persona.vue`（45 行只读）整体重写，原文件备份到 `temp/bak-persona-readonly.vue`。
- `PATCH /api/profile-items/{id}` 是字段级部分更新（逐字段 `is not None`），前端提交完整表单，显式 `null` 表示清空日期。

## 组件设计

```
frontend/src/
├── components/
│   └── ProfileItemForm.vue    新建/编辑共用表单（Modal 内）
└── pages/
    └── Persona.vue            容器：文本画像摘要 + 状态 tab + 列表 + 动作 + 历史
```

### 状态动作矩阵（唯一分发表）

| 当前 status | 可见动作 |
| --- | --- |
| `pending` | 确认（POST confirm）、拒绝（内联确认 → POST reject） |
| `confirmed` | 标记过期（内联确认 → POST expire）、编辑、删除（内联确认） |
| `expired` | 编辑、删除 |
| `rejected` | 无动作，只显示说明文案（服务端也不允许编辑） |

确认按钮的语义是"把候选升格为个人事实"（`is_user_stated=1`，`app/main.py:2348`），按钮旁固定一行说明。

### 请求体

```ts
function profileItemBody(f: any) {
  return {
    category: String(f.category ?? 'basic'),
    title: String(f.title ?? ''),
    content: String(f.content ?? '').trim(),
    confidence: Number(f.confidence ?? 1),
    source_type: String(f.source_type ?? 'manual'),
    source_detail: String(f.source_detail ?? ''),
    valid_from: f.valid_from ? String(f.valid_from) : null,
    valid_until: f.valid_until ? String(f.valid_until) : null,
  }
}
```

- 新建：`POST`，`meta` 不传（服务端默认 `{}`）。
- 编辑：`PATCH`，同上（`meta` 不传即不更新）。
- 全量提交 + 显式 `null` 日期：与服务端字段级更新兼容，且保证"清空日期"可达。

### 历史

`expandedHistory: Set<number>`；展开时请求 `GET .../history` 并缓存到 `histories: Record<number, any>`。切换 tab 不清空（条目还在时展开态保持有意义）。

### 生成推测

按钮固定旁注"需要服务端 `LLM_API_KEY`"；400/502 都走 toast 展示服务端 detail。

## 风险与取舍

| 风险 | 处理 |
| --- | --- |
| 把 rejected 条目继续暴露编辑入口 | 动作矩阵按 status 分发，rejected 无编辑；契约测试断言 |
| 「已拒绝」tab 被遗漏导致被拒条目永久不可见 | tab 列表显式包含 `已拒绝（status=rejected）` |
| confirm 语义被当成普通"打勾" | 按钮旁固定说明"确认后作为个人事实使用" |
| 日期清空不可达 | 提交显式 `null`；测试断言 `valid_from: f.valid_from ? ... : null` |
| propose 按钮让用户以为前端能开模型 | 按钮旁固定依赖说明 + 400 detail 原样展示 |
| 播种库无模型 → e2e 无法验证 propose 成功路径 | 只验证 400 detail 展示；成功路径由后端既有测试覆盖 |

## 验证计划

1. `tests/test_profile_items_ui.py`：9 条路由调用点、枚举完整性、`inference` 排除、动作矩阵、无危险 HTML + 后端契约回归（rejected PATCH 409、状态机 409、expire 的 COALESCE、history 结构、propose 400）。
2. `pytest -q` 全量。
3. `npm run build`。
4. Playwright（播种 pending + confirmed 各 1 条）：确认 / 拒绝 / 新增 / 编辑 / 历史 / propose 400 展示。
5. 真实库指纹校验。

## 回滚

- `temp/bak-persona-readonly.vue` 为原文件备份；删除 `components/ProfileItemForm.vue`、`tests/test_profile_items_ui.py`、还原 `Persona.vue` 后 `npm run build`。
- 后端改动一处（实现中发现）：`update_profile_item` 的 `valid_from`/`valid_until` 从 `is not None` 改为 `model_fields_set` 判断——原实现无法通过 API 清空日期（显式 null 与缺省等价）。备份 `temp/bak-main-py-005.py`，契约测试 `test_patch_explicit_null_clears_date_but_omitted_key_keeps_it` 固定。
