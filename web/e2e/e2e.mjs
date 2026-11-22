// 无头浏览器 e2e：两件作品的所有按钮 / 分享链路；截图到 materials/screenshots/。
// 运行：node e2e.mjs（需先 scripts/demo_up.sh；只访问 127.0.0.1）
import { chromium } from 'playwright';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const SHOTS = path.resolve('../../../materials/screenshots');
const DL = path.resolve('../../var/downloads');
fs.mkdirSync(SHOTS, { recursive: true }); fs.mkdirSync(DL, { recursive: true });
const AP = 'http://127.0.0.1:8702', GJ = 'http://127.0.0.1:8703';
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond, extra }); if (!cond) console.error('FAIL', name, extra); };

function findChromeBin() {
  const fixed = ['/usr/bin/google-chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  const hit = fixed.find((p) => fs.existsSync(p));
  if (hit) return hit;
  const cacheDir = path.join(os.homedir(), 'Library/Caches/ms-playwright');
  if (fs.existsSync(cacheDir)) {
    for (const d of fs.readdirSync(cacheDir).sort().reverse()) {
      for (const sub of [
        'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        'chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        'chrome-headless-shell-mac-arm64/chrome-headless-shell',
      ]) {
        const cand = path.join(cacheDir, d, sub);
        if (fs.existsSync(cand)) return cand;
      }
    }
  }
  return undefined;
}

const chromeBin = findChromeBin();
const browser = await chromium.launch({ ...(chromeBin ? { executablePath: chromeBin } : {}), args: ['--no-sandbox'] });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true, locale: 'zh-CN' });
await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: AP });
await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: GJ });
const errors = [];
function watch(p) {
  p.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('402')) errors.push(`${p.url()} :: ${m.text()}`); });
  p.on('pageerror', (e) => errors.push(`${p.url()} :: ${e.message}`));
  p.on('response', (r) => { if (r.status() >= 400 && !(r.status() === 402 && r.url().endsWith('/gouji/recognize'))) errors.push(`${r.status()} ${r.url()}`); });
}
const shot = (p, name, full = true) => p.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: full });
async function noHScroll(p) { return p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1); }

// ======================= Agent Pay =======================
await (await ctx.request.post(AP + '/api/reset'));
let p = await ctx.newPage(); watch(p);
await p.goto(AP + '/'); await p.waitForSelector('#m-kv dt');
check('ap: mock banner', await p.isVisible('.mock-banner'));
check('ap: phosphor font loaded', await p.evaluate(async () => { await document.fonts.ready; return [...document.fonts].some((f) => f.family.includes('Phosphor') && f.status === 'loaded'); }));
await shot(p, 'ap-01-home-1440');

await p.click('#btn-run');
await p.waitForSelector('#run-result [data-state]');
check('ap: run delivered', (await p.getAttribute('#run-result [data-state]', 'data-state')) === 'delivered');
const kinds = await p.$$eval('#run-result .timeline li', (ls) => ls.map((l) => l.dataset.kind));
check('ap: timeline discover→plan→bill→pay→deliver→receipt', ['discover', 'plan', 'bill', 'pay', 'deliver', 'receipt'].every((k) => kinds.includes(k)), kinds.join(','));
await p.waitForSelector('#receipt-sec:not([hidden]) #rc-valid.ok');
check('ap: apass 4-layer chain verified', (await p.textContent('#rc-apass')).includes('4/4') && (await p.$$('#apass-grid .apass-step.verified')).length === 4);
await shot(p, 'ap-02-delivered-timeline-1440');

await p.click('#btn-copy');
const link = await p.inputValue('#share-link');
const clip = await p.evaluate(() => navigator.clipboard.readText());
check('ap: copy share link', clip === link, clip);
const [rp] = await Promise.all([ctx.waitForEvent('page'), p.click('#btn-open')]);
watch(rp); await rp.waitForSelector('#rc-valid.ok');
check('ap: shared receipt verifies', (await rp.textContent('#rc-valid')).includes('签名有效'));
await shot(rp, 'ap-03-shared-receipt-1440'); await rp.close();

await p.fill('#task', '检查商品目录结账字段');
await p.click('#btn-run');
await p.waitForSelector('#confirm-dlg[open]');
await shot(p, 'ap-04-intent-drift-confirm-1440', false);
await p.click('#cf-yes');
await p.waitForFunction(() => document.querySelector('#run-result [data-state]')?.dataset.state === 'delivered');
check('ap: confirm then delivered', true);

await p.fill('#task', '检查商品目录结账字段');
await p.click('#btn-run'); await p.waitForSelector('#confirm-dlg[open]');
await p.click('#cf-no');
await p.waitForFunction(() => document.querySelector('#run-result [data-state]')?.dataset.state === 'rejected');
check('ap: reject works', true);

await p.fill('#task', '礼品卡');
await p.click('#btn-run');
await p.waitForFunction(() => document.querySelector('#run-result [data-state]')?.dataset.state === 'blocked');
check('ap: overreach blocked', true);

await p.uncheck('#use-llm'); await p.fill('#task', '导出商品 feed'); await p.click('#btn-run');
await p.waitForFunction(() => ['delivered', 'blocked'].includes(document.querySelector('#run-result [data-state]')?.dataset.state));
check('ap: llm toggle off still works', true);

await p.click('#btn-all');
await p.waitForFunction(() => document.querySelector('#sc-summary').textContent.includes('/9'), null, { timeout: 30000 });
const summary = await p.textContent('#sc-summary');
check('ap: 9/9 blocked', summary.includes('9/9'), summary);
await p.locator('#scenarios').scrollIntoViewIfNeeded();
await shot(p, 'ap-05-nine-scenarios-1440');
await p.click('#sc-6 button:has-text("签名同意")');
await p.waitForFunction(() => document.querySelector('#sc-6 [data-role=status]').textContent.includes('用户确认后交付'));
check('ap: scenario 6 approve', true);
await p.click('#sc-3 button:has-text("演示")');
await p.waitForFunction(() => document.querySelector('#sc-3 [data-role=status]').textContent.includes('已拦截'));
check('ap: single scenario button', true);

await p.click('#btn-verify'); await p.waitForSelector('#au-status.ok');
check('ap: audit chain ok', (await p.textContent('#au-status')).includes('哈希链完整'));
for (const id of ['#dl-jsonl', '#dl-csv']) {
  const [d] = await Promise.all([p.waitForEvent('download'), p.click(id)]);
  const f = path.join(DL, d.suggestedFilename()); await d.saveAs(f);
  const txt = fs.readFileSync(f, 'utf8');
  check(`ap: download ${d.suggestedFilename()}`, txt.length > 100 && (id === '#dl-csv' ? txt.startsWith('seq,') : txt.startsWith('{')));
}
await p.locator('#audit').scrollIntoViewIfNeeded();
await shot(p, 'ap-06-audit-1440', false);
const [disc] = await Promise.all([ctx.waitForEvent('page'), p.click('a[href="/.well-known/agent-service.json"]')]);
await disc.waitForLoadState(); check('ap: discovery doc opens', (await disc.content()).includes('agent_signature')); await disc.close();
for (const l of ['/terms', '/privacy', '/refund']) {
  const r = await p.request.get(AP + l); check(`ap: ${l}`, r.status() === 200);
}
await p.click('#btn-reset'); await p.waitForFunction(() => document.querySelector('#m-used').textContent.startsWith('¥0.00'));
check('ap: reset', true);

const ps = await ctx.newPage(); watch(ps);
await ps.goto(AP + '/?scenario=3');
await ps.waitForFunction(() => document.querySelector('#sc-3 [data-role=status]')?.textContent.includes('已拦截'));
check('ap: share link ?scenario=3 auto-runs', true); await ps.close();

for (const [w, h] of [[375, 812], [768, 1024]]) {
  const m = await browser.newContext({ viewport: { width: w, height: h }, locale: 'zh-CN' });
  const mp = await m.newPage(); watch(mp);
  await mp.goto(AP + '/'); await mp.waitForSelector('#m-kv dt');
  await mp.click('#btn-run'); await mp.waitForSelector('#run-result [data-state]');
  check(`ap: no h-scroll @${w}`, await noHScroll(mp));
  await shot(mp, `ap-home-${w}`);
  await m.close();
}

// ======================= 勾稽 =======================
const g = await ctx.newPage(); watch(g);
await g.goto(GJ + '/app.html'); await g.waitForFunction(() => document.querySelector('#bal').textContent !== '—');
await g.click('#btn-rec'); await g.waitForSelector('[data-result=quota]');
check('gj: 402 quota exhausted shown', true);
await g.click('[data-result=quota] button'); await g.waitForFunction(() => document.querySelector('#bal').textContent === '20');
check('gj: trial from quota prompt', true);

await g.goto(GJ + '/'); await g.waitForSelector('[data-pack=p500]');
check('gj: starter pack p5 visible', await g.isVisible('[data-pack=p5]'));
await shot(g, 'gj-01-pricing-1440');
await g.click('#btn-trial'); await g.waitForFunction(() => document.querySelector('#trial-msg').textContent.includes('余额'));
check('gj: trial button', (await g.textContent('#trial-msg')).includes('已领过'));
await g.click('[data-share=p500]');
const glink = await g.evaluate(() => navigator.clipboard.readText());
check('gj: copy buy link', glink.endsWith('/checkout.html?pack=p500'), glink);
await g.goto(glink); await g.waitForFunction(() => document.querySelector('#sum-total').textContent.includes('99.00'));
check('gj: share link opens checkout p500', true);
await shot(g, 'gj-02-checkout-1440');
await g.click('#btn-pay');
check('gj: consent required', await g.isVisible('#co-err'));
await g.check('#consent');
await Promise.all([g.waitForURL(/\/mock\/alipay\/cashier\//), g.click('#btn-pay')]);
check('gj: cashier has MOCK banner', (await g.textContent('[data-mock]')).includes('MOCK'));
check('gj: cashier amount', (await g.getAttribute('[data-amount]', 'data-amount')) === '99.00');
await shot(g, 'gj-03-mock-cashier-1440');
await Promise.all([g.waitForURL(/\/order\.html\?order=/), g.click('#mock-pay')]);
await g.waitForFunction(() => document.body.dataset.status === 'PAID');
check('gj: order PAID after verified notify', true);
await shot(g, 'gj-04-order-paid-1440');
await Promise.all([g.waitForURL(/app\.html/), g.click('#btn-go')]);
await g.waitForFunction(() => document.querySelector('#bal').textContent === '520');
await g.selectOption('#doc', { index: 1 });
await g.click('#btn-rec'); await g.waitForSelector('[data-result=voucher]');
const vtxt = await g.textContent('[data-result=voucher]');
check('gj: real 3-way reconciliation & red-ink rendered', vtxt.includes('48291037') && vtxt.includes('-130.00') && !vtxt.includes('_demo'), vtxt.slice(0, 120));
await g.waitForFunction(() => document.querySelector('#bal').textContent === '519');
check('gj: consume 1 credit', true);
await shot(g, 'gj-05-workbench-consume-1440');
await g.click('[data-refund]'); await g.waitForSelector('[data-result=refund]');
const rtxt = await g.textContent('[data-result=refund]');
check('gj: refund pro-rata', rtxt.includes('499') || rtxt.includes('500'), rtxt);
await g.waitForFunction(() => document.querySelector('#bal').textContent === '19' || document.querySelector('#bal').textContent === '20');
await shot(g, 'gj-06-refund-1440');
for (const sel of ['a[download="gouji-audit.jsonl"]', 'a[download="gouji-audit.csv"]']) {
  const [d] = await Promise.all([g.waitForEvent('download'), g.click(sel)]);
  const f = path.join(DL, d.suggestedFilename()); await d.saveAs(f);
  check(`gj: download ${d.suggestedFilename()}`, fs.statSync(f).size > 100);
}
for (const l of ['/terms', '/privacy', '/refund']) {
  await g.goto(GJ + l); check(`gj: ${l}`, (await g.textContent('h1')).length > 0);
}
await shot(g, 'legal-refund-1440');

for (const [w, h] of [[375, 812], [768, 1024]]) {
  const m = await browser.newContext({ viewport: { width: w, height: h }, locale: 'zh-CN' });
  const mp = await m.newPage(); watch(mp);
  for (const [u, n] of [['/', 'pricing'], ['/checkout.html?pack=p100', 'checkout'], ['/app.html', 'workbench']]) {
    await mp.goto(GJ + u); await mp.waitForLoadState('networkidle');
    check(`gj: no h-scroll ${n} @${w}`, await noHScroll(mp));
    if (w === 375) await shot(mp, `gj-${n}-375`);
  }
  await m.close();
}

await browser.close();
check('no console errors / 4xx', errors.length === 0, errors.join('\n'));
fs.writeFileSync(path.join(SHOTS, 'e2e-result.json'), JSON.stringify({ at: new Date().toISOString(), results }, null, 1));
const failed = results.filter((r) => !r.ok);
console.log(`${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
