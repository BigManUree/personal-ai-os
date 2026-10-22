/**
 * 004-recommendation-insights UI smoke.
 * Requires a temp DB seeded by temp/seed_insights.py (1 accepted + 1 pending).
 * Usage: node e2e_insights.mjs <baseUrl>
 */
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:18768";
const results = [];
function t(name, pass, detail = "") {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
const apiGet = async (path) => (await fetch(BASE + path)).json();

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 1100 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });

const accepted = () => page.locator('[data-rec-id="1"]');
const pending = () => page.locator('[data-rec-id="2"]');

await page.goto(BASE, { waitUntil: "networkidle" });
await page.locator('.nav-item:has-text("智能建议")').first().click();
await page.waitForTimeout(700);
t("进入智能建议页", (await page.evaluate(() => location.hash)) === "#/recommendations");

await accepted().waitFor({ state: "visible", timeout: 8000 });
t("accepted 卡片出现结果登记", await accepted().locator('[data-block="outcome"]').isVisible());
t("pending 卡片没有结果登记", (await pending().locator('[data-block="outcome"]').count()) === 0);
t("初始为未登记", (await accepted().innerText()).includes("未登记"));

// ---------- fill and save ----------
await accepted().locator('select[name="execution_status"]').selectOption("completed");
await accepted().locator('select[name="result_rating"]').selectOption("better");
await accepted().locator('textarea[name="actual_effect_note"]').fill("完成率从 42% 提升到 60%");
await accepted().locator('button:has-text("添加指标")').click();
await page.waitForTimeout(200);
t("指标行可添加", (await accepted().locator('select[name="metric"]').count()) === 1);
await accepted().locator('select[name="metric"]').first().selectOption("tasks_completed");
await accepted().locator('select[name="unit"]').first().selectOption("count");
await accepted().locator('input[name="expected_value"]').first().fill("5");
await accepted().locator('input[name="observed_value"]').first().fill("3");

await accepted().locator('button:has-text("保存结果")').click();
await page.waitForTimeout(1000);
t("保存后标记为已登记", (await accepted().innerText()).includes("已登记"));

const saved = await apiGet("/api/recommendations/1/outcome");
const oc = saved.outcome || {};
t("API: execution_status=completed", oc.execution_status === "completed", String(oc.execution_status));
t("API: result_rating=better", oc.result_rating === "better", String(oc.result_rating));
t("API: 备注已保存", oc.actual_effect_note === "完成率从 42% 提升到 60%");
t("API: 指标 1 条且数值正确",
  Array.isArray(oc.metric_observations) && oc.metric_observations.length === 1
  && oc.metric_observations[0].metric === "tasks_completed"
  && oc.metric_observations[0].expected_value === 5
  && oc.metric_observations[0].observed_value === 3
  && oc.metric_observations[0].source === "user_reported"
  && oc.metric_observations[0].attribution === "not_inferred",
  JSON.stringify(oc.metric_observations));

// ---------- reload and verify refill ----------
await page.reload({ waitUntil: "networkidle" });
await accepted().waitFor({ state: "visible", timeout: 8000 });
await page.waitForTimeout(500);
t("刷新后执行状态回填", (await accepted().locator('select[name="execution_status"]').inputValue()) === "completed");
t("刷新后评级回填", (await accepted().locator('select[name="result_rating"]').inputValue()) === "better");
t("刷新后备注回填", (await accepted().locator('textarea[name="actual_effect_note"]').inputValue()) === "完成率从 42% 提升到 60%");
t("刷新后指标行回填",
  (await accepted().locator('select[name="metric"]').count()) === 1
  && (await accepted().locator('select[name="metric"]').first().inputValue()) === "tasks_completed");
t("刷新后数值回填", (await accepted().locator('input[name="observed_value"]').first().inputValue()) === "3");

// ---------- statistics ----------
await page.locator('button:has-text("统计与历史")').click();
await page.waitForTimeout(1200);
const insights = page.locator('[data-section="recommendation-insights"]');
t("统计面板展开", await insights.isVisible());
let text = await insights.innerText();
t("推荐总数 = 2", text.includes("推荐总数") && /\b2\b/.test(text), text.split("\n").slice(0, 6).join(" | "));
t("当前采纳率 50.0%", text.includes("50.0%"));
t("结果登记率 100.0%", text.includes("100.0%"));
t("完成率 100.0%", text.includes("100.0%"));
t("无反馈的比率显示 —", text.includes("—"), "helpful_rate 分母为 0");
t("展示口径说明", text.includes("不代表完整历史版本"));
t("暂无生成记录", text.includes("暂无生成记录"));
t("显示样本状态", text.includes("insufficient_data") || text.includes("no_data"));
await page.screenshot({ path: "../temp/e2e-insights-filled.png", fullPage: true });

await page.locator('[data-section="recommendation-insights"] button:has-text("近 7 天")').click();
await page.waitForTimeout(1000);
text = await insights.innerText();
t("切 7 天窗口后仍有数据", text.includes("推荐总数") && /\b2\b/.test(text));

// ---------- delete outcome ----------
await accepted().locator('button:has-text("删除结果")').click();
await page.waitForTimeout(200);
t("删除需二次确认", await accepted().locator('button:has-text("确认删除结果")').isVisible());
await accepted().locator('button:has-text("确认删除结果")').click();
await page.waitForTimeout(1000);
t("删除后回到未登记", (await accepted().innerText()).includes("未登记"));
const afterDelete = await apiGet("/api/recommendations/1/outcome");
t("API: outcome 变回 null", afterDelete.outcome === null, JSON.stringify(afterDelete));

await page.screenshot({ path: "../temp/e2e-insights-final.png", fullPage: true });
t("无控制台错误 / 4xx 请求", errors.length === 0, errors.slice(0, 3).join(" ; "));

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
