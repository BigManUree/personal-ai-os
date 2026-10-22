# 实现方案：私人档案馆视觉重塑

## 触点

| 文件 | 变更 |
|---|---|
| `frontend/src/style.css` | 重写 token（亮/暗两套）+ 组件层（btn/input/card/tag/nav-item/brand-seal/tab-on）+ focus-visible + reduced-motion |
| `frontend/tailwind.config.js` | 新增 seal/seal-soft 色、font-kai 字族、圆角收紧（lg 6 / md 5 / sm 4） |
| `frontend/src/App.vue` | 壳层 7 处：品牌印「档」、侧栏墨色与分组签、审批徽标→seal、topbar 楷体页题 + 状态章、toast 语义色、主题切换按钮 |
| `frontend/src/pages/Work.vue` | 筛选 tab 选中态 `btn-primary` → `tab-on`（9 处，纯 class 替换） |
| `frontend/src/pages/Overview.vue` | 「欢迎回来」加 `font-kai`（1 处 class） |
| `frontend/index.html` | favicon：印泥朱方印「档」SVG |

## 关键取舍

- **改 token 不改布局**：13 个页面全部通过 CSS 变量继承新视觉，页面文件零逻辑改动。
- **红色收敛语义**：印泥朱 = 待裁决（审批徽标/删除确认/错误/品牌印），与 success/warning 互斥；竹青只表激活。
- **墨色实心按钮只给真正的主动作**（新建/发送），筛选选中态用 `tab-on` 竹青纸底，避免「一屏全黑」。
- **零网络字体**：楷体用 Windows 自带 `KaiTi/STKaiti` 栈；本地优先产品不引入 Google Fonts。

## 回滚

`git revert <commit>`，或还原三份备份：`temp/bak-style-012.css`、`temp/bak-app-vue-012.vue`、`temp/bak-tailwind-012.js` 后 `npm run build`。

## 工具

`frontend/ui_shot.mjs` / `ui_shot_dark.mjs`：批量截图脚本（亮/暗），供改版前后比对。
