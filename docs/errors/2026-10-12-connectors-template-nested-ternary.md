# 2026-10-12：数据源卡片定时同步行模板字符串嵌套三元导致 JS 语法错误

## 现象
3.5.3 第二批给 `app/static/connectors.js` 的数据源卡片加“定时同步已开启/下次/连续失败”展示时，
在一个反引号模板里同时写了：外层三元 + 内层 `${cond ? ... : ...}` + 字符串拼接 `+`，括号配对错位，
浏览器抛 `SyntaxError: Unexpected token ':'`。后果是整个 connectors.js 不执行：
`#dsKind` 选项为空、添加数据源表单与列表全部失效（Playwright `selectOption` 30s 超时暴露问题）。

## 根因
在模板字面量内手写多层嵌套三元并与 `+` 拼接，可读性差且容易漏/错放 `:` 与括号；
改动后没有先跑 `node --check`。

## 修复
- 抽出独立辅助函数 `scheduleLine(s)`，先算普通字符串变量再插入模板，卡片中只保留 `${scheduleLine(s)}`，
  消除嵌套三元。
- 增加纪律：改任何前端 JS 后先执行 `node --check app/static/<file>.js`，再做 Playwright 冒烟。

## 验证
`node --check` 通过；schedule_smoke.js 全流程（未开启标签→开启定时→展示间隔与下次→输入 0 关闭→删除）通过，零 console 错误。
