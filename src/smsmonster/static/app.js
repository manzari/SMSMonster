(() => {
'use strict';
const $ = (id) => document.getElementById(id);
const TK = 'smsmonster.token';
const getToken = () => { try { return localStorage.getItem(TK) || ''; } catch { return ''; } };
const setToken = (t) => { try { t ? localStorage.setItem(TK, t) : localStorage.removeItem(TK); } catch {} };

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'data') Object.assign(e.dataset, v);
    else if (k in e && k !== 'list') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}

const S = {
  auth: false, modems: new Map(), msgs: [], filter: 'all', hooks: [], hookEvents: [],
  fresh: new Set(), open: new Set(), tracking: null, atHist: {}, more: false, ignored: [],
};

function toast(msg, err) {
  const t = h('div', { class: 'toast' + (err ? ' err' : ''), role: 'status' }, msg);
  $('toasts').append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3000);
}

async function api(path, opts = {}) {
  const headers = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const tok = getToken();
  if (tok) headers.Authorization = 'Bearer ' + tok;
  let r;
  try {
    r = await fetch('api/' + path, { method: opts.method || 'GET', headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
  } catch (e) {
    toast('Cannot reach the gateway', true);
    throw e;
  }
  if (r.status === 401) { showTokenPrompt(); throw new Error('Unauthorized'); }
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) {
    const msg = (data && data.error) || r.status + ' ' + r.statusText;
    if (!opts.quiet) toast(msg, true);
    throw new Error(msg);
  }
  return data;
}

const qs = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== '' && v != null) p.set(k, v);
  const s = p.toString();
  return s ? '?' + s : '';
};

function ago(iso) {
  if (!iso) return '';
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  if (s < 86400 * 30) return Math.round(s / 86400) + ' d ago';
  return new Date(iso).toLocaleDateString();
}
const full = (iso) => (iso ? new Date(iso).toLocaleString() : '');
const timeEl = (iso) => h('time', { dateTime: iso || '', title: full(iso), data: { t: iso || '' } }, ago(iso));
setInterval(() => document.querySelectorAll('time[data-t]').forEach((t) => { if (t.dataset.t) t.textContent = ago(t.dataset.t); }), 30000);

function badge(text, cls) { return h('span', { class: 'badge ' + (cls || '') }, String(text).replace(/_/g, ' ')); }
const STATUS_CLS = {
  ready: 'b-ok', sent: 'b-ok', delivered: 'b-ok', received: 'b-info', connecting: 'b-warn', initializing: 'b-warn',
  queued: 'b-warn', sending: 'b-warn', sim_pin_required: 'b-warn', sim_locked: 'b-warn', no_sim: 'b-warn',
  disconnected: 'b-bad', error: 'b-bad', failed: 'b-bad',
};
const statusBadge = (s) => badge(s, STATUS_CLS[s]);

function modemLabel(id) {
  const m = S.modems.get(id);
  return m ? (m.name || m.phone_number || m.device || m.id) : id;
}

/* ---------- navigation ---------- */
const VIEWS = ['modems', 'messages', 'send', 'webhooks'];
function route() {
  if (!$('view-token').hidden && S.auth && !getToken()) return;
  const v = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'messages';
  VIEWS.forEach((n) => { $('view-' + n).hidden = n !== v; });
  $('view-token').hidden = true;
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === v));
  if (v === 'messages' && !S.loadedMsgs) loadMessages(true);
  if (v === 'webhooks') loadHooks();
  if (v === 'send') updateCount();
}
window.addEventListener('hashchange', route);

function showTokenPrompt() {
  VIEWS.forEach((n) => { $('view-' + n).hidden = true; });
  $('view-token').hidden = false;
  $('token-input').focus();
}

/* ---------- stats ---------- */
let statsTimer;
const refreshStats = () => { clearTimeout(statsTimer); statsTimer = setTimeout(loadStats, 400); };
async function loadStats() {
  try {
    const s = await api('stats', { quiet: true });
    for (const k of ['received', 'sent', 'failed', 'queued']) $('st-' + k).textContent = s[k] ?? 0;
  } catch {}
}

/* ---------- modems ---------- */
const SIGNAL_LEVELS = (p) => (p == null ? 0 : p > 75 ? 4 : p > 50 ? 3 : p > 25 ? 2 : p > 0 ? 1 : 0);

function modemCard(m) {
  const lv = SIGNAL_LEVELS(m.signal_percent);
  const bars = h('span', { class: 'bars ' + (lv <= 1 ? 'low' : lv === 2 ? 'mid' : ''), title: m.signal_dbm != null ? m.signal_dbm + ' dBm' : 'No signal data' },
    [1, 2, 3, 4].map((i) => h('i', { class: i <= lv ? 'on' : '' })));
  const save = async (field, input) => {
    if (input.value === (m[field] || '')) return;
    try {
      const upd = await api('modems/' + encodeURIComponent(m.id), { method: 'PATCH', body: { name: field === 'name' ? input.value : m.name || '', phone_number: field === 'phone_number' ? input.value : m.phone_number || '' } });
      upsertModem(upd);
      toast('Saved');
    } catch {}
  };
  const field = (key, ph, label) => {
    const i = h('input', { value: m[key] || '', placeholder: ph, 'aria-label': label, onchange: () => save(key, i), onkeydown: (e) => { if (e.key === 'Enter') i.blur(); } });
    return i;
  };
  const row = (k, v) => (v ? [h('dt', {}, k), h('dd', {}, v)] : []);
  const c = m.counters || {};
  const ignore = async () => {
    if (!confirm('Ignore ' + (m.device || m.id) + '?\nThe port is closed and never scanned again until you restore it.')) return;
    try { await api('modems/' + encodeURIComponent(m.id) + '/ignore', { method: 'POST' }); removeModem(m.id); loadIgnored(); toast('Port ignored'); } catch {}
  };
  return h('article', { class: 'card', data: { id: m.id, s: m.status } },
    h('div', { class: 'card-head' }, h('span', { class: 'dev' }, m.device || m.id), statusBadge(m.status),
      h('button', { class: 'ignore', title: 'Ignore this port (not a modem)', 'aria-label': 'Ignore this port', onclick: ignore }, '\u00d7')),
    h('div', { class: 'edit' }, field('name', 'Name', 'Modem name'), field('phone_number', 'Phone number', 'Phone number')),
    h('div', { class: 'sig' }, bars, m.operator || 'No operator', badge(m.registration || 'unknown', m.registration === 'home' || m.registration === 'roaming' ? 'b-ok' : m.registration === 'denied' ? 'b-bad' : '')),
    h('dl', {}, row('IMEI', m.imei), row('ICCID', m.iccid), row('Model', [m.model, m.firmware].filter(Boolean).join(' ')), row('Baud', m.baudrate), row('Last seen', m.last_seen && full(m.last_seen))),
    h('div', { class: 'counts' }, h('span', {}, 'in ', h('b', {}, c.received ?? 0)), h('span', {}, 'out ', h('b', {}, c.sent ?? 0)), h('span', {}, 'failed ', h('b', {}, c.failed ?? 0)), h('span', {}, 'queued ', h('b', {}, m.queued ?? 0))),
    m.last_error ? h('div', { class: 'err' }, m.last_error) : null,
    h('div', { class: 'actions' },
      h('button', { class: 'btn sm', onclick: () => openAt(m) }, 'AT console'),
      h('button', { class: 'btn sm', onclick: () => openUssd(m) }, 'USSD'),
      h('button', { class: 'btn sm', onclick: async () => { try { await api('modems/' + encodeURIComponent(m.id) + '/restart', { method: 'POST' }); toast('Restarting modem'); } catch {} } }, 'Restart')));
}

function upsertModem(m) {
  S.modems.set(m.id, m);
  const list = $('modem-list');
  const old = list.querySelector('[data-id="' + CSS.escape(m.id) + '"]');
  if (old && old.contains(document.activeElement)) return;
  const card = modemCard(m);
  old ? old.replaceWith(card) : list.append(card);
  afterModems();
}
function removeModem(id) {
  S.modems.delete(id);
  const old = $('modem-list').querySelector('[data-id="' + CSS.escape(id) + '"]');
  if (old) old.remove();
  afterModems();
}
function afterModems() {
  $('modem-empty').hidden = S.modems.size > 0;
  $('st-modems').textContent = S.modems.size;
  for (const sel of [$('f-modem'), $('s-modem')]) {
    const cur = sel.value;
    const first = sel.firstElementChild;
    sel.replaceChildren(first, ...[...S.modems.values()].map((m) => h('option', { value: m.id }, modemLabel(m.id) + (m.status !== 'ready' ? ' (' + m.status.replace(/_/g, ' ') + ')' : ''))));
    sel.value = cur;
  }
}
async function loadModems() {
  const list = await api('modems');
  $('modem-list').replaceChildren();
  S.modems.clear();
  list.forEach(upsertModem);
  afterModems();
  loadIgnored().catch(() => {});
}
async function loadIgnored() {
  S.ignored = await api('ignored', { quiet: true });
  renderIgnored();
}
function renderIgnored() {
  $('ignored').hidden = S.ignored.length === 0;
  $('ignored-list').replaceChildren(...S.ignored.map((d) => h('li', {},
    h('span', { class: 'dev' }, d.device),
    h('button', { class: 'btn sm', onclick: async () => {
      try { await api('ignored' + qs({ device: d.device }), { method: 'DELETE' }); toast('Port restored, scanning'); loadIgnored(); } catch {}
    } }, 'Restore'))));
}

/* ---------- AT / USSD ---------- */
let atModem = null, ussdModem = null;
function renderAt() {
  const out = $('at-out');
  out.replaceChildren(...(S.atHist[atModem.id] || []).map(([cls, t]) => h('div', { class: cls }, t)));
  out.scrollTop = out.scrollHeight;
}
function openAt(m) {
  atModem = m;
  $('at-title').textContent = 'AT console - ' + modemLabel(m.id);
  renderAt();
  $('dlg-at').showModal();
  $('at-cmd').select();
}
$('at-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const command = $('at-cmd').value.trim();
  if (!command || !atModem) return;
  const hist = (S.atHist[atModem.id] ||= []);
  hist.push(['c', '> ' + command]);
  try {
    const r = await api('modems/' + encodeURIComponent(atModem.id) + '/at', { method: 'POST', body: { command, timeout: 10 }, quiet: true });
    (r.response || []).forEach((l) => hist.push(['', l]));
  } catch (err) { hist.push(['e', err.message]); }
  renderAt();
});
function openUssd(m) {
  ussdModem = m;
  $('ussd-title').textContent = 'USSD query - ' + modemLabel(m.id);
  $('ussd-out').textContent = '';
  $('dlg-ussd').showModal();
  $('ussd-code').focus();
}
$('ussd-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = $('ussd-out');
  out.textContent = 'Waiting for network...';
  try {
    const r = await api('modems/' + encodeURIComponent(ussdModem.id) + '/ussd', { method: 'POST', body: { code: $('ussd-code').value.trim() }, quiet: true });
    out.textContent = (r.text || r.raw || '(empty response)') + (r.status ? '\n[' + r.status + ']' : '');
  } catch (err) { out.textContent = err.message; }
});
document.querySelectorAll('dialog').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

/* ---------- messages ---------- */
const lowestId = () => (S.msgs.length ? S.msgs[S.msgs.length - 1].id : null);
function matches(m) {
  const f = S.filter;
  if (f === 'in' && !(m.direction === 'in' && m.kind === 'sms')) return false;
  if (f === 'out' && m.direction !== 'out') return false;
  if (f === 'failed' && m.status !== 'failed') return false;
  if (f === 'call' && m.kind !== 'call') return false;
  const mod = $('f-modem').value;
  if (mod && m.modem_id !== mod) return false;
  const q = $('f-search').value.trim().toLowerCase();
  if (q && !((m.number || '').toLowerCase().includes(q) || (m.text || '').toLowerCase().includes(q))) return false;
  return true;
}
function query(extra) {
  const f = S.filter;
  return qs({
    direction: f === 'in' ? 'in' : f === 'out' ? 'out' : '',
    kind: f === 'in' ? 'sms' : f === 'call' ? 'call' : '',
    status: f === 'failed' ? 'failed' : '',
    modem_id: $('f-modem').value, search: $('f-search').value.trim(), limit: 50, ...extra,
  });
}
async function loadMessages(reset) {
  const rows = await api('messages' + query(reset ? {} : { before: lowestId() }));
  S.loadedMsgs = true;
  S.msgs = reset ? rows : S.msgs.concat(rows);
  S.more = rows.length >= 50;
  renderMessages();
}

function msgRow(m) {
  const isCall = m.kind === 'call';
  const open = S.open.has(m.id);
  const parts = m.parts_total > 1 ? ' (' + (m.parts_received ?? m.parts_total) + '/' + m.parts_total + ' parts)' : '';
  const acts = [];
  if (m.direction === 'in' && !isCall) acts.push(h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); reply(m); } }, 'Reply'));
  if (m.direction === 'out' && m.status === 'failed') acts.push(h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); retry(m); } }, 'Retry'));
  acts.push(h('button', { class: 'btn sm danger', onclick: (e) => { e.stopPropagation(); del(m); } }, 'Delete'));
  return h('li', { class: 'msg' + (open ? ' open' : '') + (S.fresh.has(m.id) ? ' fresh' : ''), data: { id: m.id }, onclick: () => { open ? S.open.delete(m.id) : S.open.add(m.id); renderMessages(); } },
    h('span', { class: 'dir ' + (isCall ? 'call' : m.direction), title: isCall ? 'Call' : m.direction === 'in' ? 'Received' : 'Sent' }, isCall ? 'C' : m.direction === 'in' ? '↓' : '↑'),
    h('div', { class: 'm-head' }, h('span', { class: 'm-num' }, m.number || 'unknown'), h('span', { class: 'm-meta' }, modemLabel(m.modem_id) + parts)),
    h('div', { class: 'm-side' }, statusBadge(m.status), timeEl(m.received_at || m.sent_at || m.created_at)),
    h('div', { class: 'm-text' }, isCall ? 'Missed call' : m.text || ''),
    m.error ? h('div', { class: 'm-err', style: 'grid-column:2/4' }, m.error + (m.attempts ? ' (attempts: ' + m.attempts + ')' : '')) : null,
    open ? h('div', { class: 'm-act' }, acts) : null);
}
function renderMessages() {
  $('msg-list').replaceChildren(...S.msgs.map(msgRow));
  $('msg-empty').hidden = S.msgs.length > 0;
  $('more').hidden = !S.more;
}
function reply(m) {
  $('s-to').value = m.number || '';
  location.hash = '#send';
  $('s-text').focus();
}
async function retry(m) {
  try { onMessage(await api('messages/' + m.id + '/retry', { method: 'POST' })); toast('Queued for retry'); } catch {}
}
async function del(m) {
  if (!confirm('Delete this message?')) return;
  try {
    await api('messages/' + m.id, { method: 'DELETE' });
    S.msgs = S.msgs.filter((x) => x.id !== m.id);
    renderMessages();
    refreshStats();
  } catch {}
}
function onMessage(m, isNew) {
  const i = S.msgs.findIndex((x) => x.id === m.id);
  if (i >= 0) S.msgs[i] = m;
  else if (S.loadedMsgs && matches(m)) {
    const at = S.msgs.findIndex((x) => x.id < m.id);
    S.msgs.splice(at < 0 ? S.msgs.length : at, 0, m);
    if (isNew) { S.fresh.add(m.id); setTimeout(() => S.fresh.delete(m.id), 2500); }
  }
  if (S.loadedMsgs) renderMessages();
  if (S.tracking === m.id) renderTrack(m);
  refreshStats();
}

$('chips').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  S.filter = b.dataset.f;
  document.querySelectorAll('#chips button').forEach((x) => x.classList.toggle('on', x === b));
  loadMessages(true);
});
$('f-modem').addEventListener('change', () => loadMessages(true));
let searchTimer;
$('f-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadMessages(true), 300); });
$('more').addEventListener('click', () => loadMessages(false));

/* ---------- send ---------- */
const GSM7 = new Set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿' + 'abcdefghijklmnopqrstuvwxyzäöñüà');
const GSM7_EXT = new Set('^{}\\[~]|€');
function measure(text) {
  let units = 0, gsm = true;
  for (const ch of text) {
    if (GSM7.has(ch)) units += 1;
    else if (GSM7_EXT.has(ch)) units += 2;
    else { gsm = false; break; }
  }
  if (!gsm) {
    units = text.length;
    return { gsm, units, parts: units === 0 ? 0 : units <= 70 ? 1 : Math.ceil(units / 67), per: units <= 70 ? 70 : 67 };
  }
  return { gsm, units, parts: units === 0 ? 0 : units <= 160 ? 1 : Math.ceil(units / 153), per: units <= 160 ? 160 : 153 };
}
function updateCount() {
  const t = $('s-text').value;
  const m = measure(t);
  $('s-count').textContent = t.length + '/1530 chars - ' + m.parts + ' SMS part' + (m.parts === 1 ? '' : 's') + ' - ' + (m.gsm ? 'GSM-7' : 'UCS-2 (unicode)') + ', ' + m.per + ' per part';
  $('s-count').classList.toggle('warn', !m.gsm || m.parts > 3);
}
$('s-text').addEventListener('input', updateCount);

function renderTrack(m) {
  const t = $('s-track');
  t.hidden = false;
  t.replaceChildren(statusBadge(m.status), h('span', {}, 'To ' + m.number), m.error ? h('span', { class: 'm-err' }, m.error) : null);
}
$('send-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('s-btn');
  btn.disabled = true;
  try {
    const body = { to: $('s-to').value.trim(), text: $('s-text').value };
    if ($('s-modem').value) body.modem_id = $('s-modem').value;
    const m = await api('messages', { method: 'POST', body });
    S.tracking = m.id;
    renderTrack(m);
    $('s-text').value = '';
    updateCount();
    onMessage(m, true);
  } catch {} finally { btn.disabled = false; }
});

/* ---------- webhooks ---------- */
async function loadHooks() {
  const r = await api('webhooks');
  S.hooks = r.webhooks || [];
  if (r.events && r.events.join() !== S.hookEvents.join()) {
    S.hookEvents = r.events;
    $('h-events').replaceChildren(...r.events.map((ev) => h('label', {}, h('input', { type: 'checkbox', value: ev, checked: ev.startsWith('message.received') }), ev)));
  }
  renderHooks();
}
function renderHooks() {
  $('hook-empty').hidden = S.hooks.length > 0;
  $('hook-list').replaceChildren(...S.hooks.map((w) => h('li', { class: 'hook' },
    h('div', { class: 'u' }, w.type === 'ntfy' ? h('span', { class: 'badge b-info' }, 'ntfy') : null, ' ' + w.url,
      h('div', { class: 'ev' }, (w.events || []).join(', ') + (w.secret ? (w.type === 'ntfy' ? ' - authenticated' : ' - signed') : ''))),
    h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: !!w.enabled, onchange: async (e) => {
      try { Object.assign(w, await api('webhooks/' + w.id, { method: 'PATCH', body: { enabled: e.target.checked } })); } catch { e.target.checked = !e.target.checked; }
    } }), 'Enabled'),
    h('button', { class: 'btn sm', onclick: async () => { try { await api('webhooks/' + w.id + '/test', { method: 'POST' }); toast('Test event sent'); } catch {} } }, 'Test'),
    h('button', { class: 'btn sm danger', onclick: async () => {
      if (!confirm('Delete this webhook?')) return;
      try { await api('webhooks/' + w.id, { method: 'DELETE' }); S.hooks = S.hooks.filter((x) => x.id !== w.id); renderHooks(); } catch {}
    } }, 'Delete'))));
}
const HOOK_FORMS = {
  webhook: { url: 'URL', ph: 'https://example.com/hook', secret: 'Secret (optional)' },
  ntfy: { url: 'Topic URL', ph: 'https://ntfy.sh/your-topic', secret: 'Access token or user:password (optional)' },
};
$('h-type').addEventListener('change', () => {
  const f = HOOK_FORMS[$('h-type').value] || HOOK_FORMS.webhook;
  $('h-url-label').textContent = f.url; $('h-url').placeholder = f.ph; $('h-secret-label').textContent = f.secret;
});
$('hook-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const events = [...$('h-events').querySelectorAll('input:checked')].map((i) => i.value);
  if (!events.length) return toast('Select at least one event', true);
  const body = { type: $('h-type').value, url: $('h-url').value.trim(), events, enabled: true };
  if ($('h-secret').value) body.secret = $('h-secret').value;
  try {
    await api('webhooks', { method: 'POST', body });
    $('h-url').value = ''; $('h-secret').value = '';
    toast('Webhook added');
    loadHooks();
  } catch {}
});

/* ---------- live events ---------- */
let es, backoff = 1000, retryTimer;
function setConn(state, label) {
  $('conn').dataset.state = state;
  $('conn').lastElementChild.textContent = label;
}
function connect() {
  clearTimeout(retryTimer);
  if (es) es.close();
  setConn('wait', 'connecting');
  const tok = getToken();
  es = new EventSource('api/events' + (tok ? '?token=' + encodeURIComponent(tok) : ''));
  const parse = (fn) => (ev) => { try { fn(JSON.parse(ev.data)); } catch {} };
  es.addEventListener('hello', () => { backoff = 1000; setConn('on', 'live'); loadModems().catch(() => {}); loadStats(); });
  es.addEventListener('ping', () => setConn('on', 'live'));
  es.addEventListener('modem.updated', parse((m) => { upsertModem(m); refreshStats(); }));
  es.addEventListener('modem.removed', parse((d) => removeModem(d.id)));
  for (const n of ['device.ignored', 'device.restored']) es.addEventListener(n, () => loadIgnored().catch(() => {}));
  for (const n of ['message.received', 'call.received']) es.addEventListener(n, parse((m) => onMessage(m, true)));
  for (const n of ['message.sent', 'message.failed', 'message.updated']) es.addEventListener(n, parse((m) => onMessage(m)));
  es.addEventListener('message.partial', parse((p) => toast('Receiving multipart SMS from ' + p.number + ' (' + p.seq + '/' + p.total + ')')));
  es.onerror = () => {
    es.close();
    setConn('off', 'reconnecting');
    retryTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };
}

/* ---------- boot ---------- */
$('token-form').addEventListener('submit', (e) => {
  e.preventDefault();
  setToken($('token-input').value.trim());
  $('token-input').value = '';
  start();
});
$('signout').addEventListener('click', (e) => {
  e.preventDefault();
  setToken('');
  if (es) es.close();
  setConn('off', 'offline');
  $('signout').hidden = true;
  showTokenPrompt();
});

async function start() {
  let health;
  try {
    const r = await fetch('api/health');
    health = await r.json();
  } catch { toast('Cannot reach the gateway', true); setConn('off', 'offline'); return; }
  S.auth = !!health.auth;
  $('st-ver').textContent = 'modems' + (health.version ? ' - v' + health.version : '');
  $('signout').hidden = !(S.auth && getToken());
  if (S.auth && !getToken()) return showTokenPrompt();
  try {
    await Promise.all([loadModems(), loadStats()]);
  } catch { return; }
  S.loadedMsgs = false;
  route();
  connect();
}
start();
})();
