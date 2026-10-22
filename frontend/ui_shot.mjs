import { chromium } from 'playwright'
const base = process.argv[2] || 'http://127.0.0.1:18782'
const pages = process.argv[3] ? process.argv[3].split(',') : ['overview', 'chat', 'work']
const tag = process.argv[4] || 'before'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
for (const p of pages) {
  await page.goto(base + (p === 'overview' ? '/' : '/#/' + p), { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `../temp/ui-${p}-${tag}.png` })
  console.log('shot', p)
}
await browser.close()
