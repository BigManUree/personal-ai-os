# Tasks · 006

- [x] T001 备份 `Reminders.vue` / `Analytics.vue` 到 `temp/bak-*-006.vue`
- [x] T002 `.specify/feature.json` 指向 `specs/006-snooze-and-comparison`
- [x] T003 Reminders.vue：状态中文标签 + `snoozed_until` 格式化显示
- [x] T004 Reminders.vue：`ACTIONS_BY_STATUS` 常量（对齐 `app/reminders.py` TRANSITIONS）
- [x] T005 Reminders.vue：延期内联面板（预设 + 自定义 1..10080，`snooze_minutes` 透传）
- [x] T006 Reminders.vue：取消原因选择 + 失败展示 detail 并刷新
- [x] T007 Analytics.vue：对比区块（7/30 切换、窗口范围、definition_version）
- [x] T008 Analytics.vue：指标表格（delta / percent_change / significant，null → —）
- [x] T009 `tests/test_snooze_and_comparison_ui.py`：动作表与后端 TRANSITIONS 一致性
- [x] T010 契约测试：snooze_minutes 透传、非法时长本地拦截、409 detail 展示
- [x] T011 契约测试：percent_change 空值 → 「—」，0 → 0.0%
- [x] T012 `npm run build` 并确认产物
- [x] T013 `frontend/e2e_snooze.mjs`：真实浏览器冒烟（延期 / 非法动作 / 对比表）
- [x] T014 全量 pytest + 真实库指纹比对
- [x] T015 文档同步（README / docs/*）
