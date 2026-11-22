import { chromium } from 'playwright';
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'] });
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })).newPage();
await p.goto(process.argv[2]); await p.waitForTimeout(600); if (process.env.WAITSEL) await p.waitForSelector(process.env.WAITSEL, { timeout: 120000 });
for (const sel of (process.argv[4] || '').split(',').filter(Boolean)) { await p.click(sel); await p.waitForTimeout(800); }
await p.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, Number(process?.env?.Y || 0)); }).catch(() => {}); await p.evaluate((y) => window.scrollTo(0, y), Number(process.env.Y || 0)); await p.waitForTimeout(2600);
await p.screenshot({ path: process.argv[3] }); await b.close();
