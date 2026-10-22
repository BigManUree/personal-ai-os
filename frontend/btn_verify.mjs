import { chromium } from 'playwright'
const base = process.argv[2] || 'http://127.0.0.1:8000'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const shots = [
  ['chat', '/#/chat', ['开始对话', '发送']],
  ['knowledge', '/#/knowledge', ['上传']],
  ['connectors', '/#/connectors', ['新建数据源']],
  ['settings', '/#/settings', ['保存设置']],
]
for (const [name, hash] of shots.map(s => [s[0], s[1]])) {
  await page.goto(base + hash, { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `../temp/ui-fix-${name}.png` })
  console.log('shot', name)
}
// assert every visible primary button has a border-radius
const radii = await page.$$eval('button', bs =>
  bs.map(b => ({ t: b.textContent.trim().slice(0, 8), r: getComputedStyle(b).borderRadius }))
)
const sharp = radii.filter(b => parseFloat(b.r) < 8 && b.t)
console.log('sharp buttons:', JSON.stringify(sharp))
await browser.close()
