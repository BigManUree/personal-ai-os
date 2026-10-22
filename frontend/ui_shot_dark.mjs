import { chromium } from 'playwright'
const base = process.argv[2] || 'http://127.0.0.1:18782'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await page.goto(base, { waitUntil: 'domcontentloaded' })
await page.evaluate(() => localStorage.setItem('paos-theme', 'dark'))
await page.goto(base, { waitUntil: 'networkidle' })
await page.waitForTimeout(500)
await page.screenshot({ path: '../temp/ui-overview-dark.png' })
await page.goto(base + '/#/work', { waitUntil: 'networkidle' })
await page.waitForTimeout(500)
await page.screenshot({ path: '../temp/ui-work-dark.png' })
console.log('dark shots done')
await browser.close()
