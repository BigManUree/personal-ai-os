/**
 * 003-work-crud UI smoke: create/edit a project, record an event, create a goal,
 * create a task, walk the status shortcuts, then delete everything.
 * Usage: node e2e_work.mjs <baseUrl>
 */
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:18767";
const results = [];
function t(name, pass, detail = "") {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
const apiGet = async (path) => (await fetch(BASE + path)).json();

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });

// Scoping by data-section matters: a task card also contains the project name,
// so a page-wide "hasText" filter would match the wrong row.
const projSec = page.locator('[data-section="projects"]');
const goalSec = page.locator('[data-section="goals"]');
const taskSec = page.locator('[data-section="tasks"]');
const projectCard = () => projSec.locator("div.rounded-md.border", { hasText: "E2E 项目" }).first();
const goalCard = () => goalSec.locator("div.rounded-md.border", { hasText: "E2E 目标" }).first();
const taskCard = () => taskSec.locator("div.rounded-md.border", { hasText: "E2E 任务" }).first();

await page.goto(BASE, { waitUntil: "networkidle" });
await page.locator('.nav-item:has-text("项目与任务")').first().click();
await page.waitForTimeout(600);
t("进入项目与任务页", (await page.evaluate(() => location.hash)) === "#/work");
t("三个区块渲染", await projSec.isVisible() && await goalSec.isVisible() && await taskSec.isVisible());

// ---------- project: create ----------
await page.locator('button:has-text("新建项目")').click();
await page.waitForTimeout(300);
t("新建项目弹层出现", await page.locator('input[name="name"]').isVisible());
await page.fill('input[name="name"]', "E2E 项目");
await page.fill('textarea[name="description"]', "描述保留测试");
await page.fill('input[name="tech_stack"]', "FastAPI");
await page.selectOption('select[name="status"]', "active");
await page.click('button:has-text("创建项目")');
await page.waitForTimeout(900);
t("项目已创建", await projectCard().isVisible());
t("项目卡片显示技术栈", (await projectCard().innerText()).includes("FastAPI"));

// ---------- project: edit (status only) ----------
await projectCard().locator('button:has-text("编辑")').first().click();
await page.waitForTimeout(300);
t("编辑表单回填名称", (await page.inputValue('input[name="name"]')) === "E2E 项目");
t("编辑表单回填描述", (await page.inputValue('textarea[name="description"]')) === "描述保留测试");
await page.selectOption('select[name="status"]', "completed");
await page.click('button:has-text("保存修改")');
await page.waitForTimeout(900);
let cardText = await projectCard().innerText();
t("状态变为 completed", cardText.includes("completed"));
t("仅改状态后描述仍在（全量 PATCH）", cardText.includes("描述保留测试"));
t("仅改状态后技术栈仍在", cardText.includes("FastAPI"));

// ---------- project: detail + event ----------
await projectCard().locator('button:has-text("详情")').first().click();
await page.waitForTimeout(700);
t("详情出现事件表单", await page.locator('input[name="event_content"]').isVisible());
await page.selectOption('select[name="event_type"]', "decision");
await page.fill('input[name="event_content"]', "选 SQLite 做本地存储");
await page.click('button:has-text("记录")');
await page.waitForTimeout(800);
cardText = await projectCard().innerText();
t("事件已记录", cardText.includes("选 SQLite 做本地存储"));
t("项目计数显示 1 事件", cardText.includes("1 事件"));

const projects = await apiGet("/api/projects");
const pid = projects[0].id;

// ---------- goal: create + complete ----------
await page.locator('button:has-text("新建目标")').click();
await page.waitForTimeout(300);
await page.fill('input[name="title"]', "E2E 目标");
await page.selectOption('select[name="kind"]', "milestone");
await page.fill('input[name="target_date"]', "2026-12-31");
await page.selectOption('select[name="project_id"]', { label: "E2E 项目" });
await page.click('button:has-text("创建目标")');
await page.waitForTimeout(900);
t("目标已创建", await goalCard().isVisible());
t("目标显示类型", (await goalCard().innerText()).includes("milestone"));

await goalCard().locator('button:has-text("标记完成")').first().click();
await page.waitForTimeout(1000);
t("完成后离开 active 过滤", (await goalSec.locator("div.rounded-md.border", { hasText: "E2E 目标" }).count()) === 0);
await goalSec.locator('button:has-text("全部")').first().click();
await page.waitForTimeout(800);
t("全部过滤下显示 done", (await goalCard().innerText()).includes("done"));

const goals = await apiGet("/api/goals");
const gid = goals[0].id;

// ---------- task: create ----------
await page.locator('button:has-text("新建任务")').click();
await page.waitForTimeout(300);
await page.fill('input[name="title"]', "E2E 任务");
await page.fill('textarea[name="detail"]', "别丢的细节");
await page.selectOption('select[name="priority"]', "high");
await page.fill('input[name="due_date"]', "2026-12-01");
await page.selectOption('select[name="project_id"]', { label: "E2E 项目" });
await page.selectOption('select[name="goal_id"]', { label: "E2E 目标" });
await page.click('button:has-text("创建任务")');
await page.waitForTimeout(900);
t("任务已创建", await taskCard().isVisible());
let taskText = await taskCard().innerText();
t("任务显示项目名", taskText.includes("E2E 项目"));
t("任务显示详情", taskText.includes("别丢的细节"));
t("任务显示 high 优先级", taskText.includes("high"));

// ---------- task: shortcuts ----------
await taskCard().locator('button:has-text("开始")').first().click();
await page.waitForTimeout(700);
t("开始 -> doing", (await taskCard().innerText()).includes("doing"));
await taskCard().locator('button:has-text("完成")').first().click();
await page.waitForTimeout(700);
t("完成 -> done", (await taskCard().innerText()).includes("done"));
await taskCard().locator('button:has-text("重新打开")').first().click();
await page.waitForTimeout(700);
taskText = await taskCard().innerText();
t("重开 -> todo", taskText.includes("todo"));
t("状态往返后详情未丢", taskText.includes("别丢的细节"));

// ---------- task: edit form preserves every field ----------
await taskCard().locator('button:has-text("编辑")').first().click();
await page.waitForTimeout(300);
t("编辑保留 detail", (await page.inputValue('textarea[name="detail"]')) === "别丢的细节");
t("编辑保留 due_date", (await page.inputValue('input[name="due_date"]')) === "2026-12-01");
t("编辑保留 project_id", (await page.inputValue('select[name="project_id"]')) === String(pid));
t("编辑保留 goal_id", (await page.inputValue('select[name="goal_id"]')) === String(gid));
// The task card also has a 「取消」 shortcut; scope the click to the form.
await page.locator('form button:has-text("取消")').first().click();
await page.waitForTimeout(300);

await page.screenshot({ path: "../temp/e2e-work-filled.png", fullPage: true });

// ---------- delete task / goal / project ----------
await taskCard().locator('button:has-text("删除")').first().click();
await page.waitForTimeout(200);
t("任务删除需二次确认", await page.locator('button:has-text("确认删除")').first().isVisible());
await page.locator('button:has-text("确认删除")').first().click();
await page.waitForTimeout(900);
t("任务已删除", (await taskSec.locator("div.rounded-md.border", { hasText: "E2E 任务" }).count()) === 0);

await goalCard().locator('button:has-text("删除")').first().click();
await page.waitForTimeout(200);
await page.locator('button:has-text("确认删除")').first().click();
await page.waitForTimeout(900);
t("目标已删除", (await goalSec.locator("div.rounded-md.border", { hasText: "E2E 目标" }).count()) === 0);

await projectCard().locator('button:has-text("删除")').first().click();
await page.waitForTimeout(200);
t("项目删除提示副作用", (await projectCard().innerText()).includes("事件会一并删除"));
await page.locator('button:has-text("确认删除")').first().click();
await page.waitForTimeout(1000);
t("项目已删除", (await projSec.locator("div.rounded-md.border", { hasText: "E2E 项目" }).count()) === 0);

const [p2, g2, t2] = await Promise.all([
  apiGet("/api/projects"), apiGet("/api/goals"), apiGet("/api/tasks"),
]);
t("API 中三张表已清空", p2.length === 0 && g2.length === 0 && t2.length === 0,
  `projects=${p2.length} goals=${g2.length} tasks=${t2.length}`);

await page.screenshot({ path: "../temp/e2e-work-final.png", fullPage: true });
t("无控制台错误 / 4xx 请求", errors.length === 0, errors.slice(0, 3).join(" ; "));

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
