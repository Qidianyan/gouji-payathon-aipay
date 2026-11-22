import { api, el, icon, timeline, toast, copy, year } from '/static/common.js';

const CODE_CN = {
  MANDATE_AMOUNT_PER_TX: '超过单笔上限', MANDATE_AMOUNT_TOTAL: '超过累计额度', MANDATE_MERCHANT_MISMATCH: '商户不在授权内',
  MANDATE_EXPIRED: '授权已过期', MANDATE_CATEGORY: '类目不在授权内', MANDATE_USES_EXHAUSTED: '次数用尽',
  CONFIRM_INTENT_DRIFT: '与授权意图不符', AGENT_SIGNATURE_INVALID: 'agent 签名无效', INVALID_PAYMENT_PROOF: '支付凭证无效',
  PROOF_RESOURCE_MISMATCH: '凭证与资源不符', DUPLICATE_SUPPRESSED: '重复已去重', MANDATE_AGENT_MISMATCH: 'agent 不是被授权的那个',
};
const fmtTime = (s) => new Date(s * 1000).toLocaleString('zh-CN', { hour12: false });
let S = null;

async function loadState() {
  S = (await api('/api/state')).data;
  const m = S.mandate;
  document.getElementById('m-id').textContent = `${m.mandate_id} · 用户 ${m.user_ref} · 签名 ${m.signature_alg} ${m.signature_short}`;
  const kv = document.getElementById('m-kv'); kv.replaceChildren();
  const rows = [['商户', m.merchant_ids.join('、')], ['单笔上限', `¥${m.max_amount_per_tx}`], ['累计上限', `¥${m.max_amount_total}`],
    ['类目', m.categories.join('、')], ['次数上限', `${m.max_uses} 次`], ['意图关键词', m.intent_keywords.join('、') || '不限'],
    ['绑定 agent', m.agent_keyid], ['有效至', fmtTime(m.expires_at)], ['用户公钥指纹', m.user_key_fingerprint]];
  for (const [k, v] of rows) kv.append(el('dt', { text: k }), el('dd', { class: k === '用户公钥指纹' ? 'mono' : '', text: v }));
  document.getElementById('m-used').textContent = `¥${m.used_amount} / ¥${m.max_amount_total} · ${m.used_count}/${m.max_uses} 次`;
  document.getElementById('m-meter').style.width = `${Math.min(100, (parseFloat(m.used_amount) / parseFloat(m.max_amount_total)) * 100)}%`;
  const chip = document.getElementById('planner-chip');
  chip.lastElementChild.textContent = S.llm ? `LLM：${S.llm}` : 'LLM 未配置 · 规则选品';
  const task = document.getElementById('task'); if (!task.value) task.value = S.default_task;
  renderApassBoard();
  renderScenarios();
}

function renderApassBoard(attestation = null, valid = false) {
  const grid = document.getElementById('apass-grid');
  if (!grid) return;
  const st = document.getElementById('apass-status');
  const layers = attestation?.layers || S?.apass?.layers || [];
  const DESC = {
    1: '用户 Ed25519 私钥签署预算、类目、意图与有效期边界，绑定唯一 Agent 公钥。',
    2: '商户 Admit 13 条动态准入核验通过后占额锁定商品，签发 RSA2 402 账单。',
    3: '支付宝 A402 接口核验 payment_proof 与 client_session 签发有效支付凭据。',
    4: '交付内容 SHA-256 指纹、fulfillment.confirm 回执与审计链头由商户根签名锚定。',
  };
  grid.replaceChildren(...layers.map((l) => el('div', { class: `apass-step${valid ? ' verified' : ''}`, 'data-layer': l.layer },
    el('span', { class: 'l-tag', text: `LAYER 0${l.layer} · ${l.crypto}` }),
    el('h3', { text: l.pillar }),
    el('p', { text: DESC[l.layer] || '' }),
    el('div', { class: 'hash-pill', text: l.hash ? `chain_hash: ${l.hash.slice(0, 20)}…` : '等待交易生成链式哈希…' })
  )));
  if (st) {
    if (attestation) {
      st.className = `chip ${valid ? 'ok' : 'block'}`;
      st.replaceChildren(icon(valid ? 'seal-check' : 'warning'), valid ? `四层哈希链验真通过 · root ${attestation.root_hash.slice(0, 12)}…` : '存证链校验异常');
    } else {
      st.className = 'chip';
      st.replaceChildren(icon('shield-check'), '待发起交易锚定 · 兼容 @alipay/agent-payment');
    }
  }
}

function stateChip(r) {
  const map = { delivered: ['ok', 'check-circle', '已交付'], blocked: ['block', 'hand-palm', '已拦截'],
    needs_confirmation: ['wait', 'signature', '等待用户确认'], no_match: ['', 'question', '没有匹配商品'], rejected: ['block', 'x-circle', '用户已拒绝'] };
  const [c, i, t] = map[r.state] || ['', 'info', r.state];
  return el('span', { class: `chip ${c}`, 'data-state': r.state }, icon(i), t);
}

function showRun(r) {
  const box = document.getElementById('run-result');
  box.replaceChildren(el('div', { class: 'toolbar mt-5' }, stateChip(r), r.code ? el('span', { class: 'chip block' }, CODE_CN[r.code] || r.code) : null),
    timeline(r.steps));
  if (r.state === 'needs_confirmation') openConfirm(r);
  if (r.state === 'delivered' && r.receipt) showReceipt(r.receipt.trade_no || r.receipt?.payment?.trade_no);
  loadState();
}

async function runTask(ev) {
  ev?.preventDefault();
  const btn = document.getElementById('btn-run'); btn.disabled = true;
  document.getElementById('run-result').replaceChildren(el('p', { class: 'empty-note', text: 'Agent 正在工作…' }));
  const r = await api('/api/run', { method: 'POST', body: { task: document.getElementById('task').value, use_llm: document.getElementById('use-llm').checked } });
  btn.disabled = false;
  showRun(r.data);
}

let pendingConfirm = null;
function openConfirm(r) {
  pendingConfirm = r;
  const item = S.catalog.find((c) => c.sku === r.sku);
  document.getElementById('cf-text').textContent = `Agent 想购买「${item ? item.title : r.sku}」（¥${item ? item.price : '?'}），这超出了你授权时说的用途。`;
  const codes = (r.steps.at(-1).data.codes || []).map((c) => CODE_CN[c] || c).join('、');
  document.getElementById('cf-codes').textContent = codes;
  document.getElementById('confirm-dlg').showModal();
}
async function answerConfirm(approve) {
  document.getElementById('confirm-dlg').close();
  const r = await api('/api/confirm', { method: 'POST', body: { confirmation_id: pendingConfirm.confirmation_id, approve } });
  const target = pendingConfirm._scenario;
  pendingConfirm = null;
  if (target) { appendScenario(target, r.data); return; }
  showRun(r.data);
}

async function showReceipt(tradeNo) {
  if (!tradeNo) return;
  const r = (await api(`/api/receipts/${tradeNo}`)).data;
  const sec = document.getElementById('receipt-sec'); sec.hidden = false;
  const rc = r.receipt;
  const kv = document.getElementById('rc-kv'); kv.replaceChildren();
  const rows = [['支付宝交易号', rc.trade_no], ['商户订单号', rc.out_trade_no], ['金额', `¥${rc.amount}`], ['资源', rc.bill?.resource_id],
    ['授权单', rc.mandate?.mandate_id], ['交付摘要 SHA-256', (rc.delivery_sha256 || '').slice(0, 24) + '…'], ['履约确认', rc.fulfillment_confirmed ? '已回传支付宝（MOCK）' : '未回传']];
  for (const [k, v] of rows) kv.append(el('dt', { text: k }), el('dd', { class: 'mono', text: v ?? '—' }));
  const valid = document.getElementById('rc-valid');
  valid.className = `chip ${r.signature_valid ? 'ok' : 'block'}`;
  valid.replaceChildren(icon(r.signature_valid ? 'seal-check' : 'x-circle'), r.signature_valid ? '商户签名有效（RSA2）' : '签名无效');
  const apassChip = document.getElementById('rc-apass');
  if (apassChip) {
    apassChip.className = `chip ${r.apass_valid ? 'ok' : 'block'}`;
    apassChip.replaceChildren(icon(r.apass_valid ? 'shield-check' : 'warning'), r.apass_valid ? `APASS 四层存证有效（${r.apass_verified_layers}/4）` : 'APASS 存证异常');
  }
  renderApassBoard(rc.apass_attestation, r.apass_valid);
  const link = `${location.origin}/receipt.html?trade_no=${encodeURIComponent(rc.trade_no)}`;
  document.getElementById('share-link').value = link;
  document.getElementById('btn-open').href = link;
}

function renderScenarios() {
  const grid = document.getElementById('sc-grid');
  if (grid.childElementCount) return;
  for (const s of S.scenarios) {
    const card = el('article', { class: 'card scenario', id: `sc-${s.n}`, 'data-n': s.n },
      el('span', { class: 'no', text: `0${s.n}` }), el('h3', { text: s.name }), el('p', { text: s.desc }),
      el('div', { class: 'row' }, el('span', { class: 'chip', 'data-role': 'status' }, '未运行'),
        el('button', { class: 'btn btn-secondary btn-sm', type: 'button', onclick: () => runScenario(s.n) }, icon('play'), '演示')),
      el('div', { 'data-role': 'tl' }));
    grid.append(card);
  }
}
function setScenario(n, res) {
  const card = document.getElementById(`sc-${n}`);
  const st = card.querySelector('[data-role=status]');
  const waiting = res.confirmation_id && res.code === 'CONFIRM_INTENT_DRIFT' && !res.resolved;
  st.className = `chip ${waiting ? 'wait' : res.blocked ? 'block' : ''}`;
  st.replaceChildren(icon(waiting ? 'signature' : res.blocked ? 'hand-palm' : 'warning'), waiting ? '待用户确认' : res.blocked ? `已拦截 · ${CODE_CN[res.code] || res.code}` : '未拦截');
  st.dataset.blocked = String(!!res.blocked);
  card.querySelector('[data-role=tl]').replaceChildren(timeline(res.steps));
  if (waiting) {
    card.querySelector('[data-role=tl]').append(el('div', { class: 'toolbar' },
      el('button', { class: 'btn btn-primary btn-sm', type: 'button', onclick: () => { pendingConfirm = { ...res, sku: 'catalog-audit', _scenario: n, steps: res.steps }; answerConfirm(true); } }, icon('check'), '签名同意'),
      el('button', { class: 'btn btn-secondary btn-sm', type: 'button', onclick: () => { pendingConfirm = { ...res, _scenario: n }; answerConfirm(false); } }, icon('x'), '拒绝')));
  }
}
function appendScenario(n, r) {
  const card = document.getElementById(`sc-${n}`);
  card.querySelector('[data-role=tl]').replaceChildren(timeline(r.steps));
  const st = card.querySelector('[data-role=status]');
  st.className = `chip ${r.state === 'delivered' ? 'ok' : 'block'}`;
  st.replaceChildren(icon(r.state === 'delivered' ? 'check-circle' : 'x-circle'), r.state === 'delivered' ? '用户确认后交付' : '用户拒绝，已取消');
  st.dataset.blocked = 'true';
  loadState();
}
async function runScenario(n) {
  const r = await api(`/api/scenarios/${n}`, { method: 'POST' });
  setScenario(n, r.data);
  return r.data;
}
async function runAll() {
  const btn = document.getElementById('btn-all'); btn.disabled = true;
  let blocked = 0;
  for (const s of S.scenarios) { const r = await runScenario(s.n); if (r.blocked) blocked++; }
  const sum = document.getElementById('sc-summary');
  sum.className = `chip ${blocked === 9 ? 'ok' : 'block'}`; sum.replaceChildren(icon('shield-check'), `${blocked}/9 已拦截`);
  btn.disabled = false; loadState();
}

async function verifyAudit() {
  const r = (await api('/api/audit/verify')).data;
  const c = document.getElementById('au-status');
  c.className = `chip ${r.ok ? 'ok' : 'block'}`; c.replaceChildren(icon(r.ok ? 'check-circle' : 'x-circle'), r.ok ? `哈希链完整 · ${r.count} 条` : '哈希链被篡改');
}

document.getElementById('run-form').addEventListener('submit', runTask);
document.getElementById('btn-reset').addEventListener('click', async () => {
  await api('/api/reset', { method: 'POST' });
  document.getElementById('sc-grid').replaceChildren(); document.getElementById('receipt-sec').hidden = true;
  document.getElementById('run-result').replaceChildren(el('p', { class: 'empty-note', text: '已重置：新的授权单、新的 MOCK 网关。' }));
  document.getElementById('sc-summary').className = 'chip'; document.getElementById('sc-summary').textContent = '未运行';
  await loadState(); toast('已重置');
});
document.getElementById('btn-all').addEventListener('click', runAll);
document.getElementById('btn-verify').addEventListener('click', verifyAudit);
document.getElementById('btn-copy').addEventListener('click', () => copy(document.getElementById('share-link').value));
document.getElementById('cf-yes').addEventListener('click', () => answerConfirm(true));
document.getElementById('cf-no').addEventListener('click', () => answerConfirm(false));
year();
await loadState();
const q = new URLSearchParams(location.search);
if (q.get('scenario')) { const n = +q.get('scenario'); document.getElementById('scenarios').scrollIntoView(); runScenario(n); }
if (q.get('task')) { document.getElementById('task').value = q.get('task'); runTask(); }
