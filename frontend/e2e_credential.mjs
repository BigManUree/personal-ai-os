// 007 credential browser smoke. Usage: node e2e_credential.mjs http://127.0.0.1:18771
// Requires a seeded temp db (temp/seed_credential.py): one mock source id=1.
import { chromium } from 'playwright'
import { createRequire } from 'module'

const base = process.argv[2] || 'http://127.0.0.1:18771'
const results = []
let passed = 0

function t(name, ok, extra = '') {
  results.push(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !extra ? '' : ` — ${extra}`}`)
  if (ok) passed++
}

async function api(path, options) {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json' }, ...options,
  })
  return { status: res.status, body: await res.json().catch(() => null) }
}

const errors = []
const browser = await chromium.launch()
const page = await browser.newPage()
page.on('console', (m) => {
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text())
})
page.on('response', (res) => {
  if (res.status() >= 400) errors.push(`HTTP ${res.status()} ${res.url()}`)
})

await page.goto(base, { waitUntil: 'networkidle' })
await page.locator('.nav-item:has-text("数据源")').first().click()
await page.waitForSelector('[data-source-id="1"]', { timeout: 8000 })
await page.waitForTimeout(300)

// ---------- badge starts unconfigured ----------
const badge0 = await page.locator('[data-source-id="1"] [data-field="cred-badge"]').textContent()
t('初始徽标为未配置', badge0.includes('未配置'), badge0)

// ---------- set credential via UI ----------
await page.locator('[data-source-id="1"] button[data-action="cred"]').click()
await page.waitForSelector('[data-source-id="1"] [data-panel="credential"]')
const inputType = await page.locator('[data-source-id="1"] input[name="secret"]').getAttribute('type')
t('凭据输入为 password', inputType === 'password', String(inputType))
const echoed = await page.locator('[data-source-id="1"] input[name="secret"]').inputValue()
t('输入框不回显已有值', echoed === '')

const SECRET = 'e2e_plain_pat_0099887766'
await page.locator('[data-source-id="1"] input[name="secret"]').fill(SECRET)
await page.locator('[data-source-id="1"] button[type="submit"]:has-text("保存")').click()
await page.waitForTimeout(700)

const badge1 = await page.locator('[data-source-id="1"] [data-field="cred-badge"]').textContent()
t('保存后徽标变已配置', badge1.includes('已配置'), badge1)
t('输入框已清空', (await page.locator('[data-source-id="1"] input[name="secret"]').count()) === 0)

// plaintext must not leak anywhere in the served page
const html = await page.content()
t('页面不残留明文', !html.includes(SECRET))

// db-level: ciphertext only (execFileSync avoids cmd.exe path mangling)
const { execFileSync } = createRequire(import.meta.url)('child_process')
const path = await import('node:path')
const py = path.resolve(process.cwd(), '..', '.venv', 'Scripts', 'python.exe')
const cipher = execFileSync(
  py,
  ['-c', "import sqlite3;print(sqlite3.connect(r'temp/e2e-cred.db').execute('SELECT ciphertext FROM connector_credentials').fetchone()[0])"],
  { cwd: path.resolve(process.cwd(), '..'), encoding: 'utf-8' },
).trim()
t('库中为密文不含明文', !!cipher && !cipher.includes(SECRET) && cipher.length > 40, cipher.slice(0, 20))

// clearing panel appears only after a credential exists
await page.locator('[data-source-id="1"] button[data-action="cred"]').click()
await page.waitForSelector('[data-source-id="1"] [data-panel="credential"]')
t('已配置时显示清除入口', await page.locator('[data-source-id="1"] button[data-action="cred-clear"]').isVisible())

await page.locator('[data-source-id="1"] button[data-action="cred-clear"]').click()
await page.waitForSelector('[data-source-id="1"] [data-panel="cred-clear"]')
await page.locator('[data-source-id="1"] button[data-action="cred-clear-confirm"]').click()
await page.waitForTimeout(700)
const badge2 = await page.locator('[data-source-id="1"] [data-field="cred-badge"]').textContent()
t('清除后徽标回到未配置', badge2.includes('未配置'), badge2)

// ---------- create github source with PAT ----------
await page.locator('button:has-text("新建数据源")').click()
await page.waitForSelector('select[name="kind"]')
await page.locator('select[name="kind"]').selectOption('github')
await page.fill('input[name="name"]', 'E2E 仓库')
await page.fill('input[name="owner"]', 'octocat')
await page.fill('input[name="repo"]', 'hello-world')
const PAT = 'e2e_create_pat_1122334455'
await page.fill('input[name="secret"]', PAT)
await page.locator('form ~ * button:has-text("创建"), button:has-text("创建")').last().click()
await page.waitForTimeout(800)

const created = await api('/api/data-sources')
const gh = (created.body || []).find((s) => s.name === 'E2E 仓库')
t('GitHub 源创建成功且立即已配置', !!gh && gh.has_credential === true, JSON.stringify(created.body)?.slice(0, 120))
t('列表响应不含明文 PAT', !JSON.stringify(created.body).includes(PAT))

// cleanup: delete the created source via API
if (gh) await api(`/api/data-sources/${gh.id}`, { method: 'DELETE' })

await page.screenshot({ path: '../temp/e2e-credential-final.png', fullPage: true })
t('无意外控制台错误 / 4xx', errors.length === 0, errors.slice(0, 3).join(' ; '))

await browser.close()
console.log(results.join('\n'))
console.log(`${passed}/${results.length} passed`)
process.exit(passed === results.length ? 0 : 1)
