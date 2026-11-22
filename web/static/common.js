// 演示前端公共工具（无框架）。
export async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body });
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  return { status: r.status, ok: r.ok, data };
}
export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
export const icon = (name) => el('i', { class: `ph ph-${name}`, 'aria-hidden': 'true' });
export function toast(msg) {
  let t = document.querySelector('.toast');
  if (!t) { t = el('div', { class: 'toast', role: 'status', 'aria-live': 'polite' }); document.body.append(t); }
  t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
}
export async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('链接已复制'); }
  catch { toast('复制失败，请手动复制'); }
}
const KIND = { discover: 'magnifying-glass', plan: 'robot', request: 'paper-plane-tilt', bill: 'receipt', pay: 'wallet',
  deliver: 'package', receipt: 'seal-check', blocked: 'hand-palm', confirm: 'signature', mandate: 'file-text', info: 'info' };
export function timeline(steps) {
  const ol = el('ol', { class: 'timeline' });
  for (const s of steps) {
    const cls = s.kind === 'blocked' ? 'is-blocked' : (s.kind === 'deliver' || s.kind === 'receipt') ? 'is-ok' : s.kind === 'confirm' ? 'is-wait' : '';
    const d = s.data || {};
    const detail = d.reason || d.fallback_reason ? [d.reason, d.fallback_reason && `降级原因：${d.fallback_reason}`].filter(Boolean).join(' · ')
      : (d.codes ? d.codes.join(', ') : (d.detail || ''));
    ol.append(el('li', { class: cls, 'data-kind': s.kind }, el('span', { class: 'dot' }, icon(KIND[s.kind] || 'dot-outline')),
      el('div', { class: 't', text: s.title }), detail ? el('div', { class: d.detail && !d.codes && !d.reason ? 'd mono' : 'd', text: detail }) : null));
  }
  return ol;
}
export function year() { const y = document.getElementById('year'); if (y) y.textContent = new Date().getFullYear(); }
