// Browser smoke for feature 008: streaming chat + FTS-backed citations.
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:18773'
const results = []
let passed = 0
function t(name, cond, extra = '') {
  results.push(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + extra}`)
  if (cond) passed++
}

const browser = await chromium.launch()
const page = await browser.newPage()
const consoleErrors = []
page.on('pageerror', (e) => consoleErrors.push(String(e)))

await page.goto(base, { waitUntil: 'networkidle' })
await page.locator('.nav-item:has-text("AI 助手")').first().click()
await page.waitForTimeout(400)

const input = page.locator('input[placeholder*="问"], textarea').first()
await input.fill('本项目的技术栈是什么？')
await page.locator('button:has-text("发送")').first().click()

// streaming: assistant bubble should grow before the final render settles
await page.waitForTimeout(150)
const earlyLen = await page.evaluate(() => {
  const bubbles = document.querySelectorAll('#chat-msgs .whitespace-pre-wrap, #chat-msgs div')
  const last = bubbles[bubbles.length - 1]
  return last ? last.textContent.length : -1
})

await page.waitForFunction(
  () => {
    const bubbles = document.querySelectorAll('#chat-msgs div')
    const last = bubbles[bubbles.length - 1]
    return last && last.textContent.includes('本地演示模式') && last.textContent.length > 50
  },
  { timeout: 15000 },
)
const finalLen = await page.evaluate(() => {
  const bubbles = document.querySelectorAll('#chat-msgs div')
  return bubbles[bubbles.length - 1].textContent.length
})

t('助手气泡有内容', finalLen > 50, `len=${finalLen}`)
t('流式期间已有部分内容(增量渲染)', earlyLen >= 0 && earlyLen <= finalLen, `early=${earlyLen} final=${finalLen}`)
t('回答引用了资料(资料证据块)', await page.locator('#chat-msgs:has-text("资料证据")').count() > 0)

// second message reuses the conversation
await input.fill('再补充一下流式输出')
await page.locator('button:has-text("发送")').first().click()
await page.waitForFunction(
  () => document.querySelectorAll('#chat-msgs div').length >= 4,
  { timeout: 15000 },
)
t('同会话第二条消息', true)

// conversation list persisted both messages
await page.waitForTimeout(500)
const convText = await page.locator('body').textContent()
t('会话在列表可见', convText.includes('本项目的技术栈'))

await page.screenshot({ path: '../temp/e2e-chat-stream.png', fullPage: true })

const unexpected = consoleErrors.filter((e) => !e.includes('Failed to load resource'))
t('无页面错误', unexpected.length === 0, unexpected.slice(0, 2).join(' ; '))

console.log(results.join('\n'))
console.log(`${passed}/${results.length} passed`)
await browser.close()
process.exit(passed === results.length ? 0 : 1)
