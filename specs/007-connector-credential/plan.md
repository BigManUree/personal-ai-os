# Plan · 007

## 组件

| 组件 | 职责 |
|---|---|
| `frontend/src/pages/Connectors.vue` | 凭据徽标、设置/清除面板、GitHub PAT 创建字段 |
| `tests/test_connector_credential_ui.py` | 前端接线断言 + 后端凭据契约回归 |
| `frontend/e2e_credential.mjs` + `temp/seed_credential.py` | 浏览器冒烟 |

后端零改动。

## 关键实现点

- 凭据状态来自 `GET /api/data-sources` 的 `has_credential`；PUT/DELETE 后直接用响应更新对应卡片，不整页刷新（避免打断面板状态）。
- `credSecret` 单一 ref 服务于面板输入；提交成功后立即清空。编辑已有源时 PAT 字段不渲染（FR-204）。
- 明文防御：`type="password"`、无 `:value` 回显、提交后清空、页面源码断言「无 secret 出现在列表渲染」。

## 验证

1. `pytest tests/test_connector_credential_ui.py -q`
2. `pytest -q` 全量
3. `npm run build`
4. Playwright 冒烟（播种一个 github 源，18771 端口）：UI 设置凭据 → DB ciphertext 校验 → 清除 → 创建带 PAT → 已配置。
5. 真实库指纹比对。

## 回滚

- `temp/bak-connectors-vue-007.vue` 为改动前备份。
- 删 `tests/test_connector_credential_ui.py`、`frontend/e2e_credential.mjs`、`temp/seed_credential.py`，还原 Connectors.vue，`npm run build` 即回到改动前。
