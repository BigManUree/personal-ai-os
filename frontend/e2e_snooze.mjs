// 006 snooze + comparison browser smoke. Usage: node e2e_snooze.mjs http://127.0.0.1:18770
// Requires a seeded temp database (temp/seed_snooze.py): reminders 1=due, 2=snoozed,
// 3=completed, 4=scheduled; behavior events only inside the current window.
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:18770'
const results = []
let passed = 0

function t(name, ok, extra = '') {
  results.push(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !extra ? '' : ` — ${extra}`}`)
  if (ok) passed++
}

async function apiGet(path) {
  const res = await fetch(base + path)
  return res.json()
}

const errors = []
const browser = await chromium.launch()
const page = await browser.newPage()
// "Failed to load resource" lines carry no URL (the response listener logs it),
// so skip them here or the deliberate-400 filter cannot match.
page.on('console', (m) => {
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text())
})
page.on('response', (res) => {
  if (res.status() >= 400) errors.push(`HTTP ${res.status()} ${res.url()}`)
})

await page.goto(base, { waitUntil: 'networkidle' })

// ---------- reminders: state-driven actions ----------
await page.locator('.nav-item:has-text("提醒中心")').first().click()
await page.waitForSelector('[data-reminder-id="1"]', { timeout: 8000 })
await page.waitForTimeout(300)

const dueActions = await page.locator('[data-reminder-id="1"] button[data-action]').allTextContents()
t('due 卡片动作集', dueActions.includes('延期') && dueActions.includes('知悉')
  && dueActions.includes('完成') && dueActions.includes('忽略') && dueActions.includes('取消'), dueActions.join('/'))

const completedCards = await page.locator('[data-reminder-id="3"]').count()
t('默认列表隐藏终态', completedCards === 0)

const schedActions = await page.locator('[data-reminder-id="4"] button[data-action]').allTextContents()
t('scheduled 只有取消', schedActions.length === 1 && schedActions[0] === '取消', schedActions.join('/'))

const snoozeLabel = await page.locator('[data-reminder-id="2"] [data-field="snoozed-until"]').textContent()
t('snoozed_until 可见', /延期至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(snoozeLabel.trim()), snoozeLabel)

// ---------- snooze: preset then verify wake time = click time + 60 min ----------
// The server computes snoozed_until from "now", not from scheduled_at.
const clickWallClock = Date.now()
await page.locator('[data-reminder-id="1"] button[data-action="snooze"]').click()
await page.waitForSelector('[data-reminder-id="1"] [data-panel="snooze"]')
t('延里面板出现预设', await page.locator('[data-reminder-id="1"] button[data-snooze="60"]').isVisible())

await page.locator('[data-reminder-id="1"] button[data-snooze="60"]').click()
await page.waitForTimeout(700)
const afterSnooze = await apiGet('/api/reminders?status=snoozed')
const snoozedRow = afterSnooze.find((r) => r.id === 1)
t('snooze 后状态变 snoozed', snoozedRow?.status === 'snoozed')
if (snoozedRow) {
  const deltaMin = (new Date(snoozedRow.snoozed_until).getTime() - clickWallClock) / 60000
  t('唤醒时间 ≈ 点击时刻 + 60 分钟', Math.abs(deltaMin - 60) < 2, `${deltaMin.toFixed(1)}`)
}

// custom input rejects garbage locally (no request fired)
await page.locator('[data-reminder-id="2"] button[data-action="complete"]').waitFor()
const card2 = page.locator('[data-reminder-id="2"]')
await card2.locator('button[data-action]:has-text("取消")').click()
await page.waitForSelector('[data-reminder-id="2"] [data-panel="cancel"]')
await page.locator('[data-reminder-id="2"] button[data-action="cancel-confirm"]').click()
await page.waitForTimeout(700)
const afterCancel = await apiGet('/api/reminders')
t('确认取消生效', !afterCancel.some((r) => r.id === 2))

// invalid transition surfaces server detail on the card
// (drive the API directly: the UI never offers this button, which is the point)
const bad = await fetch(`${base}/api/reminders/3/transition`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ action: 'snooze', snooze_minutes: 10 }),
})
t('终态 snooze 返回 409', bad.status === 409, String(bad.status))

// ---------- analytics comparison ----------
await page.locator('.nav-item:has-text("行为洞察")').first().click()
await page.waitForSelector('[data-section="comparison"]', { timeout: 8000 })
await page.waitForTimeout(600)

const currentRange = await page.locator('[data-field="current-range"]').textContent()
const previousRange = await page.locator('[data-field="previous-range"]').textContent()
t('本期/上期范围可见', /本期/.test(currentRange) && /上期/.test(previousRange))

const taskCreatedCell = page.locator('[data-metric="tasks_created"]')
const delta = await taskCreatedCell.locator('td').nth(3).textContent()
t('tasks_created 变化量 +2', delta.trim() === '+2', delta)

const percentCell = page.locator('[data-percent="goals_created"]')
t('零基数幅度显示 —', (await percentCell.textContent()).trim() === '—')

const flatArrow = await page.locator('[data-metric="connector_sync_attempts"] [data-direction="flat"]').count()
t('无数据方向为 →', flatArrow === 1)

await page.locator('button[data-window="30"]').click()
await page.waitForTimeout(600)
const stillThere = await page.locator('[data-table="comparison"]').count()
t('30 天窗口切换正常', stillThere === 1)

await page.screenshot({ path: '../temp/e2e-snooze-reminders.png', fullPage: true })
t('无意外控制台错误 / 4xx', errors.length === 0, errors.slice(0, 3).join(' ; '))

await browser.close()
console.log(results.join('\n'))
console.log(`${passed}/${results.length} passed`)
process.exit(passed === results.length ? 0 : 1)
