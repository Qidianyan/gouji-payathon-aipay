// S+ 高清演示录屏（原生 1920x1080，零小猫吉祥物，真实双服务实时演练，节奏精准对齐配音时长）
// 用法：node record.mjs vibepay | agentpay（需先启动本地服务 8702 与 8703）
import { chromium } from 'playwright';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const id = process.argv[2];
if (!['vibepay', 'agentpay'].includes(id)) {
  console.error('Usage: node record.mjs vibepay|agentpay');
  process.exit(1);
}

const V = path.resolve('../../var/video');
const OUT = path.join(V, id);
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
const TMP_DL = path.join(OUT, 'downloads');
fs.mkdirSync(TMP_DL, { recursive: true });

const timed = JSON.parse(fs.readFileSync(path.join(V, 'films_splus_timed.json'), 'utf8'));
const film = timed.films.find((f) => f.id === id);
const AP = 'http://127.0.0.1:8702';
const GJ = 'http://127.0.0.1:8703';
const GAP = 300;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
const browser = await chromium.launch({
  ...(chromeBin ? { executablePath: chromeBin } : {}),
  args: ['--no-sandbox', '--font-render-hinting=none'],
});

// 1) 渲染干净无小猫的 1920x1080 片头 / 片尾卡与右上角 elidot 品牌角标 PNG
const CARD = 'file://' + path.resolve('../../video/card.html');
{
  const c = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await c.goto(`${CARD}?kind=intro&${new URLSearchParams(film.card_intro)}`);
  await c.waitForLoadState('networkidle');
  await sleep(350);
  await c.screenshot({ path: path.join(OUT, 'card_intro.png') });

  await c.goto(`${CARD}?kind=outro&${new URLSearchParams(film.card_outro)}`);
  await c.waitForLoadState('networkidle');
  await sleep(350);
  await c.screenshot({ path: path.join(OUT, 'card_outro.png') });

  // 渲染右上角干净透明 elidot-mark 角标（用于录屏段右上角叠加，零小猫）
  const markSvg = fs.readFileSync(path.resolve('../../video/brand-logo/elidot-mark.svg'), 'utf8');
  await c.setViewportSize({ width: 168, height: 52 });
  await c.setContent(`<!doctype html><html><body style="margin:0;background:transparent;display:flex;align-items:center;justify-content:center;height:52px;font-family:'Hiragino Sans GB','PingFang SC',sans-serif;">
    <div style="display:flex;align-items:center;gap:10px;background:rgba(246,246,241,0.92);border:1px solid #C6C5BB;border-radius:999px;padding:6px 16px 6px 10px;">
      <div style="width:26px;height:26px;display:flex;align-items:center;justify-content:center;">${markSvg.replace('<svg ', '<svg width="26" height="26" ')}</div>
      <span style="font-size:16px;font-weight:700;letter-spacing:0.04em;color:#131311;">elidot</span>
    </div>
  </body></html>`);
  await sleep(150);
  await c.screenshot({ path: path.join(OUT, 'watermark.png'), omitBackground: true });
  await c.close();
}

// 2) 原生 1920x1080 高清录屏
const ctx = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  locale: 'zh-CN',
  acceptDownloads: true,
  recordVideo: { dir: OUT, size: { width: 1920, height: 1080 } },
});
for (const o of [AP, GJ]) {
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: o });
}

if (id === 'agentpay') {
  await ctx.request.post(AP + '/api/reset');
}

const p = await ctx.newPage();
// 适度放大页面比例使 1920x1080 成片中所有发票字号、表格与 APASS 哈希链清晰易读
await p.addInitScript(() => {
  document.addEventListener('DOMContentLoaded', () => {
    if (document.body) document.body.style.zoom = '1.16';
  });
});

const t0 = Date.now();
const cuts = [];
let segCut = 0;
async function waitSilent(fn, maxIdleMs = 5500) {
  const a = Date.now() - t0;
  await fn();
  const b = Date.now() - t0;
  if (b - a > maxIdleMs) {
    // 仅裁剪视觉模型网络等待空隙，保留真实起止动作与完整结果
    const cutStart = a + 1500;
    const cutEnd = b - 900;
    if (cutEnd > cutStart + 500) {
      cuts.push([cutStart, cutEnd]);
      segCut += cutEnd - cutStart;
    }
  }
}
const scrollToSel = (sel, block = 'start') =>
  p.evaluate(([s, b]) => document.querySelector(s)?.scrollIntoView({ behavior: 'smooth', block: b }), [sel, block]);
const stateIs = (s) =>
  p.waitForFunction((x) => document.querySelector('#run-result [data-state]')?.dataset.state === x, s, { timeout: 30000 });

let receiptLink = '';
let lastPngPath = '';
let lastPdfPath = '';
const brokenPdfPath = path.join(TMP_DL, 'broken-invoice.pdf');
fs.writeFileSync(brokenPdfPath, '%PDF-1.4-corrupted-stream-test');

const ACT = {
  // ======================= 成片 A：Vibe Pay · 勾稽 =======================
  async gj_pricing_top() {
    await p.goto(GJ + '/');
    await p.waitForSelector('[data-pack=p5]');
    await sleep(600);
    await p.hover('[data-buy=p5]');
  },
  async gj_pricing_tiers() {
    await scrollToSel('#plans', 'center');
    await sleep(800);
    await p.hover('[data-pack=p500]');
    await sleep(900);
    await p.hover('[data-pack=p2000]');
  },
  async gj_quota402() {
    await p.goto(GJ + '/app.html');
    await p.waitForFunction(() => document.querySelector('#bal')?.textContent !== '—');
    await sleep(700);
    await p.click('#btn-rec');
    await p.waitForSelector('[data-result=quota]');
    await scrollToSel('[data-result=quota]', 'center');
  },
  async gj_checkout() {
    await p.goto(GJ + '/');
    await p.waitForSelector('[data-buy=p5]');
    await sleep(700);
    await Promise.all([p.waitForURL(/checkout\.html\?pack=p5/), p.click('[data-buy=p5]')]);
    await p.waitForFunction(() => document.querySelector('#sum-total')?.textContent?.includes('0.99'));
    await sleep(1000);
    await p.check('#consent');
    await sleep(500);
  },
  async gj_cashier() {
    await Promise.all([p.waitForURL(/\/mock\/alipay\/cashier\//), p.click('#btn-pay')]);
    await p.waitForSelector('#mock-pay');
    await sleep(2200);
    await Promise.all([p.waitForURL(/\/order\.html\?order=/), p.click('#mock-pay')]);
  },
  async gj_paid() {
    await p.waitForFunction(() => document.body.dataset.status === 'PAID');
    await sleep(2000);
    await p.hover('#btn-go');
  },
  async gj_upload_png() {
    await Promise.all([p.waitForURL(/app\.html/), p.click('#btn-go')]);
    await p.waitForFunction(() => document.querySelector('#bal')?.textContent === '5');
    await sleep(500);
    // 下载真实数电发票 PNG 并展开高级上传真实识别（触发 gemini-4-carbon-high 多模态）
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#dl-sample-png')]);
    lastPngPath = path.join(TMP_DL, dl.suggestedFilename());
    await dl.saveAs(lastPngPath);
    await p.locator('.sub-details summary').click();
    await sleep(400);
    await p.setInputFiles('#inv-file', lastPngPath);
    await sleep(400);
    await p.click('#btn-rec');
    await waitSilent(() => p.waitForSelector('[data-result=voucher]', { timeout: 30000 }), 6000);
  },
  async gj_matched_result() {
    await p.waitForFunction(() => document.querySelector('#bal')?.textContent === '4');
    await scrollToSel('[data-result=voucher]', 'center');
    await sleep(1200);
  },
  async gj_upload_pdf_redink() {
    await scrollToSel('#doc', 'center');
    await sleep(400);
    await p.selectOption('#doc', 'office-spec-overbill');
    await sleep(400);
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#dl-sample-pdf')]);
    lastPdfPath = path.join(TMP_DL, dl.suggestedFilename());
    await dl.saveAs(lastPdfPath);
    await p.setInputFiles('#inv-file', lastPdfPath);
    await sleep(500);
    await p.click('#btn-rec');
    await p.waitForFunction(() => document.querySelector('[data-result=voucher]')?.textContent?.includes('-130.00'), null, { timeout: 15000 });
  },
  async gj_redink_detail() {
    await p.waitForFunction(() => document.querySelector('#bal')?.textContent === '3');
    await scrollToSel('[data-result=voucher]', 'center');
    await sleep(1400);
    await p.mouse.wheel(0, 260);
  },
  async gj_export_csv() {
    const csvBtn = p.locator('[data-result=voucher] button:has-text("导出金蝶")');
    await csvBtn.scrollIntoViewIfNeeded();
    await sleep(600);
    const [dl] = await Promise.all([p.waitForEvent('download'), csvBtn.click()]);
    await dl.saveAs(path.join(TMP_DL, dl.suggestedFilename()));
    await sleep(800);
  },
  async gj_parse_fail_422() {
    await scrollToSel('#inv-file', 'center');
    await sleep(400);
    await p.setInputFiles('#inv-file', brokenPdfPath);
    await sleep(500);
    await p.click('#btn-rec');
    await p.waitForSelector('.callout-danger', { timeout: 10000 });
    await scrollToSel('.callout-danger', 'center');
    await sleep(1000);
  },
  async gj_refund_and_audit() {
    // 折叠高级输入区，让左侧退款结果与右侧订单表/余额同时同框呈现
    const open = await p.locator('.sub-details').evaluate((el) => el.open);
    if (open) await p.locator('.sub-details summary').click();
    await sleep(300);
    await scrollToSel('[data-refund]', 'center');
    await sleep(700);
    await p.click('[data-refund]');
    await p.waitForSelector('[data-result=refund]', { timeout: 10000 });
    await p.waitForFunction(() => document.querySelector('#bal')?.textContent === '0');
    await scrollToSel('[data-result=refund]', 'center');
    await sleep(900);
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('a[download="gouji-audit.jsonl"]')]);
    await dl.saveAs(path.join(TMP_DL, dl.suggestedFilename()));
  },

  // ======================= 成片 B：Agent Pay · Admit × Facetry =======================
  async ap_mandate() {
    await p.goto(AP + '/');
    await p.waitForSelector('#m-kv dt');
    await p.waitForFunction(() => document.querySelector('#planner-chip')?.textContent?.includes('gemini'));
    await scrollToSel('#mandate', 'center');
    await sleep(900);
  },
  async ap_discovery() {
    const resp = await ctx.request.get(AP + '/.well-known/agent-service.json');
    const pretty = JSON.stringify(await resp.json(), null, 2)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    await p.setContent(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
    <style>
      body{margin:0;padding:56px 110px;background:#F6F6F1;color:#131311;font-family:"Hiragino Sans GB","PingFang SC",sans-serif;}
      .top{display:flex;align-items:center;gap:14px;margin-bottom:24px;}
      .badge{background:#FDEBE5;color:#B8300F;border:1.5px solid #F7401D;border-radius:999px;padding:6px 18px;font-size:18px;font-weight:700;}
      h1{margin:0;font-size:32px;color:#131311;}
      pre{margin:0;background:#FBFBF8;border:1.5px solid #C6C5BB;border-radius:16px;padding:30px 36px;font-family:"SF Mono",Menlo,monospace;font-size:19px;line-height:1.58;white-space:pre-wrap;color:#131311;box-shadow:0 16px 40px -20px rgba(19,19,17,0.16);}
    </style></head><body>
      <div class="top"><span class="badge">GET /.well-known/agent-service.json</span><h1>服务发现与 APASS / alipay-bot 协议声明</h1></div>
      <pre>${pretty}</pre>
    </body></html>`);
    await sleep(2200);
  },
  async ap_run_live() {
    await p.goto(AP + '/');
    await p.waitForSelector('#m-kv dt');
    await scrollToSel('#run', 'start');
    await sleep(400);
    const v = await p.inputValue('#task');
    await p.fill('#task', '');
    await p.type('#task', v, { delay: 32 });
    await p.check('#use-llm');
    await sleep(300);
    await p.click('#btn-run');
    await stateIs('delivered');
  },
  async ap_timeline_402() {
    await scrollToSel('#run-result', 'center');
    await sleep(1400);
  },
  async ap_apass_board() {
    await p.waitForSelector('#apass-grid .apass-step.verified');
    await scrollToSel('#apass-sec', 'center');
    await sleep(1800);
  },
  async ap_receipt_share() {
    await p.waitForSelector('#receipt-sec:not([hidden]) #rc-valid.ok');
    await scrollToSel('#receipt-sec', 'center');
    await sleep(1000);
    await p.click('#btn-copy');
    receiptLink = await p.inputValue('#share-link');
    await sleep(900);
    await p.goto(receiptLink);
    await p.waitForSelector('#rc-valid.ok');
    await p.waitForSelector('#rc-apass.ok');
    await sleep(1200);
    await scrollToSel('#rc-apass-grid', 'center');
  },
  async ap_intent_drift() {
    await p.goto(AP + '/');
    await p.waitForSelector('#m-kv dt');
    await scrollToSel('#task', 'center');
    await p.fill('#task', '');
    await p.type('#task', '检查商品目录结账字段', { delay: 45 });
    await sleep(300);
    await p.click('#btn-run');
    await p.waitForSelector('#confirm-dlg[open]', { timeout: 25000 });
  },
  async ap_confirm_sign() {
    await sleep(1800);
    await p.click('#cf-yes');
    await stateIs('delivered');
    await scrollToSel('#run-result', 'center');
    await sleep(900);
  },
  async ap_scenarios_run() {
    await scrollToSel('#scenarios', 'start');
    await sleep(600);
    await p.click('#btn-all');
    await p.waitForFunction(() => document.querySelector('#sc-summary')?.textContent?.includes('9/9'), null, { timeout: 30000 });
  },
  async ap_scenarios_done() {
    await scrollToSel('#sc-5', 'center');
    await sleep(1200);
    await scrollToSel('#sc-9', 'center');
  },
  async ap_audit_export() {
    await scrollToSel('#audit', 'center');
    await sleep(700);
    await p.click('#btn-verify');
    await p.waitForSelector('#au-status.ok');
    await sleep(800);
    const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#dl-jsonl')]);
    await dl.saveAs(path.join(TMP_DL, dl.suggestedFilename()));
  },
};

// 先把页面预热到初始画面，避免白屏
await p.goto(id === 'agentpay' ? AP + '/' : GJ + '/');
await p.waitForLoadState('networkidle');
await sleep(400);

const cues = [];
const screenLines = film.lines.map((l, i) => ({ ...l, i })).filter((l) => !l.screen.startsWith('card_'));
const lead = Date.now() - t0;

for (const l of screenLines) {
  const start = Date.now() - t0;
  cues.push({ i: l.i, start, screen: l.screen });
  segCut = 0;
  await ACT[l.screen].call(ACT);
  const elapsed = Date.now() - t0 - start - segCut;
  const targetMs = l.dur * 1000 + GAP;
  const left = targetMs - elapsed;
  console.log(`[${id}] #${l.i} ${l.screen} action=${(elapsed / 1000).toFixed(2)}s cut=${(segCut / 1000).toFixed(2)}s target=${(targetMs / 1000).toFixed(2)}s`);
  if (left > 0) await sleep(left);
}

await sleep(700);
const end = Date.now() - t0;
const vid = p.video();
await ctx.close();
const vp = await vid.path();
fs.renameSync(vp, path.join(OUT, 'screen.webm'));
await browser.close();

fs.writeFileSync(path.join(OUT, 'cues.json'), JSON.stringify({ lead, end, cues, cuts }, null, 2));
console.log(`RECORD_DONE ${id}: screen=${((end - lead) / 1000).toFixed(2)}s cues=${cues.length} cuts=${cuts.length}`);
