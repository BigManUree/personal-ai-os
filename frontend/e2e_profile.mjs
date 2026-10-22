/**
 * 005-profile-items UI smoke.
 * Requires a temp DB seeded by temp/seed_profile.py (1 pending + 1 confirmed + 1 rejected).
 * Usage: node e2e_profile.mjs <baseUrl>
 */
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:18769";
const results = [];
function t(name, pass, detail = "") {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
const apiGet = async (path) => (await fetch(BASE + path)).json();

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 1100 } });
const errors = [];
// "Failed to load resource" lines carry no URL (the response listener below
// logs the URL), so skip them here or the propose-400 filter cannot match.
page.on("console", (m) => {
  if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text());
});
page.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });

const section = () => page.locator('[data-section="profile-items"]');
const item = (id) => page.locator(`[data-item-id="${id}"]`);

await page.goto(BASE, { waitUntil: "networkidle" });
await page.locator('.nav-item:has-text("个人画像")').first().click();
await page.waitForTimeout(700);
t("进入个人画像页", (await page.evaluate(() => location.hash)) === "#/persona");

await item(1).waitFor({ state: "visible", timeout: 8000 });
t("pending 候选可见", (await item(1).innerText()).includes("pending"));
t("候选有确认/拒绝按钮", (await item(1).locator('button:has-text("确认")').first().isVisible()));
t("rejected 条目默认隐藏", (await item(3).count()) === 0);
t("档案文本摘要折叠可见", await page.locator('details:has-text("档案文本摘要")').isVisible());

// ---------- confirm pending candidate ----------
await item(1).locator('button:has-text("确认")').first().click();
await page.waitForTimeout(900);
t("候选确认后变为 confirmed", (await item(1).innerText()).includes("confirmed"));
t("出现用户确认标记", (await item(1).innerText()).includes("用户确认"));
const afterConfirm = (await apiGet("/api/profile-items")).find(x => x.id === 1);
t("API: is_user_stated = true", afterConfirm?.is_user_stated === true, JSON.stringify(afterConfirm?.is_user_stated));

// ---------- create a new item ----------
await page.locator('button:has-text("新增条目")').click();
await page.waitForTimeout(300);
await page.selectOption('select[name="category"]', "skill");
await page.fill('textarea[name="content"]', "熟悉 Playwright 端到端验证");
await page.fill('input[name="valid_from"]', "2026-10-22");
// The topbar button and the in-modal submit share the label; scope to the form.
await page.locator('form button:has-text("新增条目")').click();
await page.waitForTimeout(900);
const created = (await apiGet("/api/profile-items")).find(x => x.content === "熟悉 Playwright 端到端验证");
t("新增条目直接生效", !!created && created.status === "confirmed", JSON.stringify(created?.status));
t("新增条目出现在列表", !!(await page.locator(`[data-item-id="${created?.id}"]`).count()));

// ---------- edit the created item ----------
await page.locator(`[data-item-id="${created.id}"]`).locator('button:has-text("编辑")').click();
await page.waitForTimeout(300);
t("编辑表单回填内容", (await page.inputValue('textarea[name="content"]')) === "熟悉 Playwright 端到端验证");
t("编辑表单回填日期", (await page.inputValue('input[name="valid_from"]')) === "2026-10-22");
await page.fill('textarea[name="content"]', "熟悉 Playwright 与临时库冒烟");
await page.fill('input[name="valid_from"]', "");
await page.click('button:has-text("保存修改")');
await page.waitForTimeout(900);
const afterEdit = (await apiGet("/api/profile-items")).find(x => x.id === created.id);
t("编辑后内容更新", afterEdit?.content === "熟悉 Playwright 与临时库冒烟");
t("清空日期提交 null", afterEdit?.valid_from === null, String(afterEdit?.valid_from));

// ---------- expire the confirmed seeded item ----------
await item(2).locator('button:has-text("标记过期")').click();
await page.waitForTimeout(200);
await item(2).locator('button:has-text("确认过期")').click();
await page.waitForTimeout(900);
t("过期后状态 expired", (await item(2).innerText()).includes("expired"));
t("过期条目仍可编辑", await item(2).locator('button:has-text("编辑")').isVisible());

// ---------- reject flow needs a pending item: propose is unavailable without a key ----------
await page.locator('button:has-text("生成推测")').click();
await page.waitForTimeout(600);
t("无 Key 时展示服务端说明",
  await page.locator('div:has-text("未配置 LLM_API_KEY")').last().isVisible());

// ---------- history ----------
await item(1).locator('button:has-text("历史")').click();
await page.waitForTimeout(700);
const histText = await item(1).innerText();
t("历史展示 confirm 事件", histText.includes("confirm"));
t("历史展示时间戳", /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(histText));

// ---------- rejected tab ----------
await section().locator('button:has-text("已拒绝")').click();
await page.waitForTimeout(800);
t("已拒绝 tab 显示被拒条目", await item(3).isVisible());
t("被拒条目无编辑入口", (await item(3).locator('button:has-text("编辑")').count()) === 0);
t("被拒条目显示说明", (await item(3).innerText()).includes("已拒绝的条目不可编辑"));

await page.screenshot({ path: "../temp/e2e-profile-final.png", fullPage: true });
// The deliberate propose-400 (no API key on this instance) shows up as a
// console/resource error; it is expected noise, not a defect.
const unexpected = errors.filter((e) => !e.includes("/profile-items/propose"));
t("无意外控制台错误 / 4xx 请求", unexpected.length === 0, unexpected.slice(0, 3).join(" ; "));

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
