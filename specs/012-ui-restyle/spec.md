# 私人档案馆 — 视觉重塑

## 动机

现有 UI 是通用 SaaS 管理壳（靛蓝主色 / 灰边框 / 均质圆角卡片 / Inter），与「本地优先个人档案系统」的产品气质无关。本次把视觉语言接到产品本体：记忆、证据、批注、裁决。

## FR-1 设计 token（style.css `:root` / `.dark`）

| token | 亮色 | 暗色 | 语义 |
|---|---|---|---|
| `--bg` 瓷青纸 | `238 242 239` | `16 24 21` | 冷青调纸面 / 墨玉底 |
| `--surface` 宣白 | `251 252 251` | `26 37 33` | 卡片 |
| `--fg` 松烟墨 | `31 45 40` | `223 232 227` | 墨绿黑 |
| `--accent` 竹青 | `46 107 94` | `79 168 146` | 激活/链接/聚焦 |
| `--seal` 印泥朱 | `194 71 46` | `224 106 80` | 待裁决：审批徽标/删除确认/错误/品牌印 |
| `--sidebar-bg` | 松烟墨 `31 45 40` | 墨玉 `13 20 18` | 侧栏实色（唯一视觉重心） |
| success/warning | 苔绿 `63 125 78` / 藤黄 `183 121 31` | 提亮 | 保持语义 |

约束：红色只有一个意思（待裁决）；不引入渐变装饰；零网络字体。

## FR-2 字体

- 正文：系统黑体栈（PingFang SC / MiSans / Segoe UI / Microsoft YaHei）。
- 标题（侧栏品牌、topbar 页题、卡片/区块 h3）：楷体栈 `Kaiti SC, STKaiti, KaiTi, serif`。
- 数字统计：保持无衬线 + `font-variant-numeric: tabular-nums`。
- mono 不变。

## FR-3 组件层（@layer components）

- 圆角：lg 6px / md 5px / sm 4px（tailwind.config 覆盖）。
- 卡片：纸面 + 墨线（`--border` 改为墨色 14% 透明度等效 RGB），无阴影。
- `.tag` → 图章式：1px 描边 + 8% 同色底。
- `.btn-primary` → 墨色实心（纸色文字）；竹青用于 active 态与聚焦环。
- `.nav-item` → 侧栏内纸色文字，active 用竹青亮色 + 左侧 2px 印记线。
- `.input` focus → 竹青环。
- toast / badge 颜色对齐新语义（审批徽标用 seal）。

## FR-4 App.vue 结构性调整（仅壳层，不触碰页面逻辑）

- 品牌区：印泥朱方印「档」（楷体）+ 品牌名。
- topbar：页题楷体；「本地运行」tag → 带绿点的状态章。
- toast：success/warn/error 用新 token。
- favicon：印泥朱方印 SVG。

## 非目标

- 不改任何页面业务逻辑、布局结构、API 调用。
- 不引入网络字体、图标库、新依赖。
- 不做逐页像素级重排（页面通过 token 继承新视觉）。

## 验收

1. `npm run build` 成功。
2. 全量 pytest 通过（前端安全断言等不受影响）。
3. Playwright 截图（总览/AI 助手/项目与任务，亮色+深色）人工比对：侧栏墨色、纸面冷青、楷体标题、图章标签生效。
4. 键盘 Tab 焦点环可见；`prefers-reduced-motion` 下无入场动画。
