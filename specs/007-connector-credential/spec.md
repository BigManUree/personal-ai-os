# 007 · 连接器凭据管理

更新时间：2026-10-22

## 背景

后端已有完整凭据链路：`PUT/DELETE /api/data-sources/{id}/credential`（`app/main.py:2816-2841`）、创建时可选 `secret`（`DataSourceIn`，一次写入永不回显）、Fernet 落库（`app/secret_crypto.py`）。`Connectors.vue` 前端零调用——GitHub 数据源无法配置只读 PAT，已配置与否在界面上也看不见（`has_credential` 无任何使用点）。

## 目标

连接器卡片可见凭据状态，可设置/更换/清除凭据；创建 GitHub 数据源时可一并提交 PAT。明文永不回显、永不入列表接口。

## 功能需求

- **FR-201** 卡片显示凭据状态徽标：`has_credential` → 「凭据已配置」/「未配置」。
- **FR-202** 「设置凭据」展开内联面板：`<input type="password">`，提交 `PUT /api/data-sources/{id}/credential` `{secret}`；成功后用响应对象更新该卡片的 `has_credential`，并**清空输入框**。
- **FR-203** 「清除凭据」内联二次确认，`DELETE /api/data-sources/{id}/credential`；成功后状态回到「未配置」。
- **FR-204** 新建表单在 `kind === 'github'` 时提供可选 PAT 字段，走 `POST /api/data-sources` 的 `secret` 字段；编辑已有数据源时**不出现**该字段（后端 PATCH 也不接受 secret，凭据只能走专用端点）。
- **FR-205** 明文约束：密码框不回显任何已有值；`secret` 不写入 `config_json`；任何列表/详情渲染不引用 secret。
- **FR-206** 失败（404 源不存在 / 422 空值或超长）展示后端 detail。

## 非目标

- 不做凭据有效性测试按钮（同步失败已有 last_error 通道）。
- 不做多凭据、不做凭据轮换提醒。
- 不改加密层与路由语义。

## 验收

- 契约测试：PUT 后 `connector_credentials.ciphertext` 不含明文、`last_error` 被清空、重复 PUT 替换为单行、DELETE 后 `has_credential=false`、明文不出现在 `/api/data-sources` 响应。
- 浏览器冒烟：设置凭据 → 徽标变已配置 → 清除 → 未配置；创建 GitHub 源带 PAT → 立即已配置。
