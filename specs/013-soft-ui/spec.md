# Spec 013：Soft UI 视觉重塑（替换 012「私人档案馆」）

## 背景

用户对 feature 012 的档案风视觉不满意，指定改为 Soft UI（柔和界面风）风格套件，并提供完整 Hard Prompt（token 字典 + 禁止项 + 自检清单）。本 spec 记录替换决策与验证；012 的设计稿保留在 `specs/012-ui-restyle/` 作为历史。

## 用户需求（FR）

1. **FR-101** 主圆角 `rounded-2xl`/`rounded-3xl`；卡片 `rounded-2xl` + 白底 + 彩色柔影（`0 8px 24px rgb(15 23 42 / 0.06)`），悬浮上浮加深（`translateY(-2px)` + `0 16px 40px`）。
2. **FR-102** 背景浅色 `slate-50`，卡片纯白；文字 gray-800（标题）/gray-600（正文）/gray-400（弱化）。
3. **FR-103** 主色低饱和 indigo（500 交互 / 600 按钮），按钮含彩色阴影 `rgb(79 70 229 / 0.28)` 与 `hover:-translate-y-0.5 hover:shadow-xl`、`active:scale-97`。
4. **FR-104** 输入框 `border-0`、灰底（gray-50）、聚焦白底 + 大半径低不透明度 indigo 柔光环。
5. **FR-105** 侧栏从深墨实色改为白色 + 柔影（`shadow-shell`），选中项 `indigo-50` 底 + indigo 文字，无指示条。
6. **FR-106** 去楷体（012 遗留）：页题/品牌/卡片标题回归 `font-semibold` 系统黑体；favicon 改 indigo 大圆角方。
7. **FR-107** 图标/品牌块圆形或大圆角底（rounded-2xl / rounded-full）。

## 禁止项落实（STYLEKIT [FORBIDDEN]）

- `rounded-none` / `rounded-sm`（tailwind sm 重定义为 0.625rem，仍是圆角）/ `border-2` / `border-black` / `shadow-[`（任意值阴影全部收敛进 tailwind `boxShadow.shell` 或 CSS 内联带模糊阴影）/ `bg-black` → 全仓 grep 零命中（两处遮罩 `bg-black/30|40` 改 `bg-slate-900/30|40`）。
- 无硬阴影（所有阴影带 ≥12px 模糊）；无高饱和纯色主色（indigo-500/600 属 STYLEKIT 指定主色）。

## 保留不变

- 组件类名（`card/btn-*/input/tag/nav-item/tab-on/brand-seal`）与页面结构零改动——13 页经 CSS 变量继承。
- `seal` 语义保留（错误/待裁决徽标），色值从印泥朱改 rose-500/600。
- 深色模式同构：slate-900 底 + slate-800 卡 + indigo-400。
- 质量线：`:focus-visible` 柔光环、`prefers-reduced-motion` 全局降级、`tabular-nums`、无网络字体。

## 触点

| 文件 | 变更 |
|---|---|
| `frontend/src/style.css` | token 全量重写（新增 `--accent-strong`、`--input-bg`）+ 组件层重写 |
| `frontend/tailwind.config.js` | 圆角 lg/md/sm → 14/12/10px；新增 `boxShadow.shell`；删 `fontFamily.kai` |
| `frontend/src/App.vue` | 6 处：品牌/分组签去楷体、侧栏加 `shadow-shell`、主题按钮回归 `nav-item`（删内联 onmouseover）、页题去楷体、遮罩 `bg-slate-900/30` |
| `frontend/src/pages/Overview.vue` | 「欢迎回来」去 `font-kai` |
| `frontend/src/components/Modal.vue` | 遮罩 `bg-black/40` → `bg-slate-900/40` |
| `frontend/index.html` | favicon `#C2472E` rx18 → `#6366F1` rx28，去楷体字体声明 |

## 验证

- `npm run build` ✓ 3.31s
- `pytest -q` → 579 passed, 1 skipped（零回归；`onclick=`/`v-html` 断言不受影响——顺带删掉了 012 引入的内联 `onmouseover`）
- Playwright `e2e_snooze.mjs`（重播种临时库 18783）→ 15/15
- 截图自审：`temp/ui-{overview,chat,work}-soft.png` + `ui-overview-dark.png` / `ui-work-dark.png`

## 回滚

`git revert <commit>`；或还原 `temp/bak-style-soft-before.css`、`temp/bak-app-vue-soft-before.vue`、`temp/bak-tailwind-soft-before.js` 后 `npm run build`（回到 012 档案风）。
