/**
 * Batch 12: Full frontend regression E2E (v2 - robust selectors).
 */
import { chromium } from "playwright";
import { writeFileSync as wf } from "fs";

const BASE = "http://127.0.0.1:18765";
const results = [];
function t(name, pass, detail = "") {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();

const consoleErrors = [];
const network404 = [];
page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("response", r => { if (r.status() === 404) network404.push(r.url()); });

// ============ 1. PAGE ROUTING ============
console.log("\n=== 1. PAGE ROUTING ===");
await page.goto(BASE);
await page.waitForLoadState("networkidle");
t("首页加载", await page.locator("#app").isVisible());

const navItems = [
  ["overview","总览"],["chat","AI 助手"],["memories","长期记忆"],
  ["knowledge","知识库"],["connectors","数据源"],["work","项目与任务"],
  ["persona","个人画像"],["analytics","行为洞察"],["recommendations","智能建议"],
  ["profile","个人档案"],["approvals","操作审批"],["reminders","提醒中心"],["settings","设置"],
];
for (const [id, label] of navItems) {
  await page.locator(`.nav-item:has-text("${label}")`).first().click();
  await page.waitForTimeout(250);
  const hash = await page.evaluate(() => location.hash);
  t(`导航: ${label}`, hash === `#/${id}`, hash);
}

// Direct hash access
await page.goto(`${BASE}/#/knowledge`);
await page.waitForTimeout(500);
t("直接访问 #/knowledge", (await page.locator("h2").textContent()).includes("知识库"));

await page.goto(`${BASE}/#/chat`);
await page.waitForTimeout(500);
t("直接访问 #/chat", await page.locator("#chat-msgs").isVisible());

await page.goto(`${BASE}/#/memories`);
await page.waitForTimeout(500);
t("直接访问 #/memories", (await page.locator("h2").textContent()).includes("记忆"));

// Refresh
await page.goto(`${BASE}/#/connectors`);
await page.waitForTimeout(300);
await page.reload();
await page.waitForTimeout(500);
t("刷新保留路由", (await page.evaluate(() => location.hash)) === "#/connectors");

// Back/forward
await page.goto(`${BASE}/#/overview`);
await page.waitForTimeout(200);
await page.goto(`${BASE}/#/knowledge`);
await page.waitForTimeout(200);
await page.goBack();
await page.waitForTimeout(500);
t("后退", (await page.evaluate(() => location.hash)) === "#/overview");
await page.goForward();
await page.waitForTimeout(500);
t("前进", (await page.evaluate(() => location.hash)) === "#/knowledge");

// Unknown hash
await page.goto(`${BASE}/#/xyz`);
await page.waitForTimeout(500);
const body0 = await page.locator("body").textContent();
t("未知hash回退", body0.includes("欢迎") || body0.includes("总览"));

// ============ 2. CHAT ============
console.log("\n=== 2. CHAT ===");
await page.route("**/api/chat", r => r.fulfill({
  status: 200, contentType: "application/json",
  body: JSON.stringify({ conversation_id: "c1", answer: "MOCK_REPLY_OK" }),
}));
await page.goto(`${BASE}/#/chat`);
await page.waitForTimeout(500);
await page.fill('input[placeholder*="问问"]', "test msg");
await page.click('button[type="submit"]');
await page.waitForTimeout(800);
t("Chat正常回复", (await page.locator("#chat-msgs").textContent()).includes("MOCK_REPLY_OK"));
await page.unroute("**/api/chat");

// HTTP 500
await page.route("**/api/chat", r => r.fulfill({
  status: 500, contentType: "application/json",
  body: JSON.stringify({ detail: "ERR_500_DETAIL" }),
}));
await page.fill('input[placeholder*="问问"]', "trigger500");
await page.click('button[type="submit"]');
await page.waitForTimeout(1000);
const err500 = await page.locator("#chat-msgs").textContent();
t("Chat 500显示ERR_500_DETAIL", err500.includes("ERR_500_DETAIL") && !err500.includes("undefined"));
await page.unroute("**/api/chat");

// Network failure
await page.route("**/api/chat", r => r.abort("failed"));
await page.fill('input[placeholder*="问问"]', "netfail");
await page.click('button[type="submit"]');
await page.waitForTimeout(1000);
const errNet = await page.locator("#chat-msgs").textContent();
t("Chat网络失败", errNet.includes("Failed to fetch") && !errNet.includes("undefined"));
await page.unroute("**/api/chat");
t("失败后按钮恢复", !(await page.locator('button[type="submit"]').isDisabled()));

// ============ 3. MEMORIES ============
console.log("\n=== 3. MEMORIES ===");
await page.goto(`${BASE}/#/memories`);
await page.waitForTimeout(1000);
const memBody = await page.locator("body").textContent();
t("记忆列表加载", memBody.includes("E2E seed memory"));

// Add memory - scoped via placeholder
await page.locator('textarea[placeholder*="例如"]').fill("NEW_MEMORY_E2E");
const memAdd = page.waitForResponse(r => r.url().includes("/api/memories") && r.status() === 200);
await page.locator('button:has-text("保存")').first().click();
await memAdd;
await page.waitForTimeout(500);
t("新增记忆", (await page.locator("body").textContent()).includes("NEW_MEMORY_E2E"));

// ============ 4. KNOWLEDGE ============
console.log("\n=== 4. KNOWLEDGE ===");
const knowLoad = page.waitForResponse(r => r.url().includes("/api/documents") && r.status() === 200);
await page.goto(`${BASE}/#/knowledge`);
await knowLoad;
await page.waitForTimeout(300);
t("文档列表加载", (await page.locator("body").textContent()).includes("e2e-seed"));

// Paste text
await page.locator('input[placeholder*="资料名称"]').fill("PASTE_E2E.md");
await page.locator('textarea[placeholder*="粘贴内容"]').fill("PASTE_BODY_E2E");
const pastePost = page.waitForResponse(r => r.url().includes("/api/documents/text") && r.status() === 200);
await page.locator('button:has-text("保存到知识库")').click();
await pastePost;
await page.waitForTimeout(500);
t("粘贴文本保存", (await page.locator("body").textContent()).includes("PASTE_E2E"));

// File type rejection
const badExe = "C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\paos_batch12\\bad.exe";
wf(badExe, "x");
await page.setInputFiles('input[type="file"]', badExe);
await page.waitForTimeout(500);
t("类型拦截", (await page.locator("body").textContent()).includes("不支持"));

// Oversized
const bigMd = "C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\paos_batch12\\big.md";
wf(bigMd, Buffer.alloc(10485761));
await page.setInputFiles('input[type="file"]', bigMd);
await page.waitForTimeout(500);
t("超限拦截", (await page.locator("body").textContent()).includes("10MB"));

// At limit - use a fresh page (avoids file input state from prior oversized test)
const exactPage = await browser.newPage();
const exactMd = "C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\paos_batch12\\exact.md";
wf(exactMd, Buffer.alloc(10485760));
await exactPage.goto(`${BASE}/#/knowledge`);
await exactPage.waitForLoadState("networkidle");
await exactPage.setInputFiles('input[type="file"]', exactMd);
await exactPage.waitForTimeout(600);
const exactBody = await exactPage.locator("body").textContent();
t("恰好10MiB不拦截", exactBody.includes("已选择") && !exactBody.includes("超过"));
await exactPage.close();

// ============ 5. CONNECTORS ============
console.log("\n=== 5. CONNECTORS ===");
await page.goto(`${BASE}/#/connectors`);
await page.waitForTimeout(800);
t("数据源列表", (await page.locator("body").textContent()).includes("Mock"));

await page.click('button:has-text("新建数据源")');
await page.waitForTimeout(300);
t("新建表单", await page.locator('h3:has-text("新建数据源")').isVisible());

await page.locator('button:has-text("创建")').click();
await page.waitForTimeout(500);
t("空名称校验", (await page.locator("body").textContent()).includes("名称"));

await page.click('button:has-text("取消")');
await page.waitForTimeout(300);
t("取消", !(await page.locator('h3:has-text("新建数据源")').isVisible().catch(() => false)));

// ============ 6. RECOMMENDATIONS ============
console.log("\n=== 6. RECOMMENDATIONS ===");
await page.goto(`${BASE}/#/recommendations`);
await page.waitForTimeout(2000);
const recBody = await page.locator("body").textContent();
t("推荐列表显示", recBody.includes("测试推荐"));

const card1 = page.locator(".card").filter({ hasText: "测试推荐" }).first();
await card1.locator('button:has-text("有用")').click();
await page.waitForTimeout(1000);
t("helpful投票", (await card1.locator('button:has-text("有用")').getAttribute("class")).includes("btn-primary"));

await card1.locator('button:has-text("备注")').click();
await page.waitForTimeout(300);
await card1.locator("textarea").fill("NOTE_E2E_VALUE");
const notePut = page.waitForResponse(r => r.url().includes("/feedback") && r.status() === 200);
await card1.locator('button:has-text("保存备注")').click();
await notePut;
await page.waitForTimeout(500);
t("备注保存", (await page.locator("body").textContent()).includes("备注已保存"));

await page.reload();
await page.waitForTimeout(2000);
const c1 = page.locator(".card").filter({ hasText: "测试推荐" }).first();
await c1.locator('button:has-text("备注")').click();
await page.waitForTimeout(300);
const noteVal = await c1.locator("textarea").inputValue();
t("刷新后备注回显", noteVal.includes("NOTE_E2E_VALUE"), noteVal);

const allCards = page.locator(".card").filter({ hasText: "测试推荐" });
await allCards.nth(1).locator('button:has-text("无用")').click();
await page.waitForTimeout(1000);
t("unhelpful投票", (await allCards.nth(1).locator('button:has-text("无用")').getAttribute("class")).includes("btn-primary"));

// ============ 7. OTHER PAGES ============
console.log("\n=== 7. OTHER PAGES ===");
await page.goto(`${BASE}/#/approvals`);
await page.waitForTimeout(800);
t("审批加载", (await page.locator("body").textContent()).includes("memory.write"));

await page.goto(`${BASE}/#/reminders`);
await page.waitForTimeout(800);
const remBody = await page.locator("body").textContent();
t("提醒加载", remBody.includes("回顾") || remBody.includes("提醒"));
const remTrans = page.waitForResponse(r => r.url().includes("/transition") && r.status() === 200).catch(()=>null);
await page.locator('button:has-text("知悉")').first().click().catch(() => {});
await remTrans;
await page.waitForTimeout(500);
t("提醒知悉", (await page.locator("body").textContent()).includes("操作成功"));

await page.goto(`${BASE}/#/settings`);
await page.waitForTimeout(800);
t("设置加载", (await page.locator("body").textContent()).includes("提醒规则"));

for (const [id, kw] of [["overview","欢迎"],["work","项目"],["persona","画像"],["analytics","洞察"],["profile","档案"]]) {
  await page.goto(`${BASE}/#/${id}`);
  await page.waitForTimeout(800);
  t(`${id}页面`, (await page.locator("body").textContent()).includes(kw));
}

// ============ CONSOLE / NETWORK ============
console.log("\n=== CONSOLE/NETWORK ===");
const unexpectedErrors = consoleErrors.filter(e => !e.includes("500") && !e.includes("ERR_FAILED"));
t("Console无意外错误", unexpectedErrors.length === 0, unexpectedErrors.slice(0,2).join("; "));
t("无404", network404.length === 0, network404.slice(0,2).join("; "));

// ============ SUMMARY ============
const passed = results.filter(r => r.pass).length;
const failed = results.filter(r => !r.pass).length;
console.log(`\n=== ${passed} PASS / ${failed} FAIL / ${results.length} TOTAL ===`);
await browser.close();
process.exit(failed > 0 ? 1 : 0);
