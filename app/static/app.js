'use strict';
// Chatter Radar web UI — vanilla JS, no build step.

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v; // only used with static strings
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
// DOM .append() would print "null"/"false" for skipped optional children — filter them out.
const put = (el, ...kids) => el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false && k !== ''));
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? { 'content-type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  if (!r.ok) { const t = await r.text(); toast(`Error: ${t.slice(0, 120)}`); throw new Error(t); }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}
function toast(msg, actions) {
  // actions: a function (shown as "Undo") or a list of {label, fn}
  if (typeof actions === 'function') actions = [{ label: 'Undo', fn: actions }];
  const el = h('div', { class: 'toast' }, msg,
    (actions || []).map((a) => h('button', { onclick: () => { a.fn(); el.remove(); } }, a.label)));
  $('#toast').append(el);
  setTimeout(() => el.remove(), (actions || []).length > 1 ? 9000 : 5000);
}
const ICON = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  cal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  undo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/></svg>',
  flag: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 22V4a1 1 0 0 1 1-1h12l-2 5 2 5H5"/></svg>',
  pay: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>',
};
const icon = (name) => h('span', { html: ICON[name], style: { display: 'inline-flex' } });

const TZ = undefined; // the browser's own time zone
const todayISO = () => new Intl.DateTimeFormat('en-CA', { timeZone: TZ }).format(new Date());
const addDays = (iso, n) => { const d = new Date(iso + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
function fmtDate(iso) {
  if (!iso) return '';
  const t = todayISO();
  if (iso === t) return 'Today';
  if (iso === addDays(t, 1)) return 'Tomorrow';
  if (iso === addDays(t, -1)) return 'Yesterday';
  return new Date(iso + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
}
function fmtStamp(ts) {
  const d = new Date(ts);
  const day = new Intl.DateTimeFormat('en-CA', { timeZone: TZ }).format(d);
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: TZ });
  return day === todayISO() ? time : `${fmtDate(day)} ${time}`;
}
const money = (a, c) => a == null ? null : `${{ ILS: '₪', USD: '$', EUR: '€', GBP: '£' }[c] || (c ? c + ' ' : '')}${Number(a).toLocaleString('en-US', { maximumFractionDigits: 2 })}`;

// ---------------------------------------------------------------- state & routing
const state = { tab: 'tasks', taskStatus: 'open', taskCat: '', taskSource: '', msgSource: '', msgQuery: '', msgOnly: '', categories: [], sources: [] };
try { Object.assign(state, JSON.parse(localStorage.getItem('radar.ui') || '{}')); } catch { /* ignore */ }
const save = () => { try { localStorage.setItem('radar.ui', JSON.stringify({ tab: state.tab, taskStatus: state.taskStatus, msgSource: state.msgSource, convId: state.convId })); } catch { /* ignore */ } };

async function loadMeta() {
  [state.categories, state.sources] = await Promise.all([api('/api/categories'), api('/api/sources')]);
}
const catByKey = (k) => state.categories.find((c) => c.key === k) || { key: k, name: k, color: '#6b7280' };

function setTab(tab) {
  state.tab = tab; save();
  if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
  for (const b of document.querySelectorAll('#tabs button')) b.setAttribute('aria-current', b.dataset.tab === tab ? 'page' : 'false');
  render();
}
document.getElementById('tabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setTab(b.dataset.tab); });

async function render() {
  const view = $('#view');
  try { if (await runSetupWizard(view)) return; } catch (e) { console.error(e); document.body.classList.remove('setup-mode'); }
  const fn = { tasks: renderTasks, messages: renderMessages, sources: renderSources, settings: renderSettings, assistant: renderAssistant }[state.tab] || renderTasks;
  try { await fn(view); } catch (e) { console.error(e); }
  refreshCount();
}
async function refreshCount() {
  const [s, ins] = await Promise.all([api('/api/stats'), api('/api/instructions')]);
  const el = $('#open-count');
  el.hidden = !s.totals.open_tasks; el.textContent = s.totals.open_tasks;
  const pc = $('#proposal-count');
  pc.hidden = !ins.pending; pc.textContent = ins.pending; pc.title = 'Proposals waiting for your approval';
  state.instr = ins;
}

// ---------------------------------------------------------------- tasks
function segmented(options, value, onChange) {
  return h('div', { class: 'seg', role: 'group' }, options.map(([v, label]) =>
    h('button', { 'aria-pressed': String(v === value), onclick: () => onChange(v) }, label)));
}
function catSelect(value, onChange, { all = true } = {}) {
  return h('select', { onchange: (e) => onChange(e.target.value) },
    all && h('option', { value: '' }, 'All categories'),
    state.categories.filter((c) => c.enabled || c.key === value).map((c) => h('option', { value: c.key, selected: c.key === value }, c.name)));
}
function sourceSelect(value, onChange, label = 'All sources') {
  return h('select', { onchange: (e) => onChange(e.target.value) },
    h('option', { value: '' }, label),
    state.sources.map((s) => h('option', { value: String(s.id), selected: String(s.id) === String(value) }, s.name)));
}

function bucket(t) {
  const today = todayISO();
  if (!t.due_date) return 'No date';
  if (t.due_date < today) return 'Overdue';
  if (t.due_date === today) return 'Today';
  if (t.due_date === addDays(today, 1)) return 'Tomorrow';
  if (t.due_date <= addDays(today, 7)) return 'This week';
  return 'Later';
}

// ---------------------------------------------------------------- first-run setup wizard
// Shown instead of the whole app until the three steps are done. `?preview=key|waiting|blocked|chats` shows a step
// with made-up status, so you can see each screen without redoing the earlier ones.
const PREVIEW = new URLSearchParams(location.search).get('preview');
document.body.classList.add('setup-mode');
let setupTimer = null;

const FDA_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles';
const FDA_SVG = `<svg viewBox="0 0 420 250" role="img" aria-label="Drag the file into the Full Disk Access list, or click the plus button, and switch it on">
  <rect x="1" y="1" width="418" height="248" rx="12" style="fill:var(--surface-2);stroke:var(--border)"/>
  <rect x="16" y="14" width="268" height="222" rx="10" style="fill:var(--surface);stroke:var(--border)"/>
  <circle cx="32" cy="29" r="4.5" fill="#ff5f57"/><circle cx="46" cy="29" r="4.5" fill="#febc2e"/><circle cx="60" cy="29" r="4.5" fill="#28c840"/>
  <text x="150" y="33" text-anchor="middle" font-size="12" font-weight="600" style="fill:var(--text)">Full Disk Access</text>
  <line x1="16" y1="43" x2="284" y2="43" style="stroke:var(--border)"/>
  <text x="30" y="62" font-size="10" style="fill:var(--muted)">Allow the applications below to access all data.</text>
  <rect x="30" y="72" width="240" height="116" rx="6" style="fill:var(--bg);stroke:var(--border)"/>
  <rect x="40" y="82" width="16" height="16" rx="4" style="fill:var(--border)"/><rect x="64" y="86" width="70" height="8" rx="4" style="fill:var(--border)"/>
  <rect x="226" y="82" width="30" height="16" rx="8" style="fill:var(--border)"/><circle cx="234" cy="90" r="6" fill="#fff"/>
  <rect x="34" y="106" width="232" height="30" rx="5" style="fill:var(--accent-soft);stroke:var(--accent)" stroke-dasharray="4 3"/>
  <rect x="40" y="113" width="16" height="16" rx="4" style="fill:var(--accent)"/><text x="64" y="125" font-size="11" font-weight="600" style="fill:var(--text)">python3.12</text>
  <rect x="226" y="113" width="30" height="16" rx="8" style="fill:var(--ok)"/><circle cx="248" cy="121" r="6" fill="#fff"/>
  <rect x="40" y="144" width="16" height="16" rx="4" style="fill:var(--border)"/><rect x="64" y="148" width="90" height="8" rx="4" style="fill:var(--border)"/>
  <rect x="226" y="144" width="30" height="16" rx="8" style="fill:var(--border)"/><circle cx="234" cy="152" r="6" fill="#fff"/>
  <rect x="30" y="188" width="240" height="26" style="fill:var(--surface-2);stroke:var(--border)"/>
  <rect x="32" y="190" width="26" height="22" rx="4" style="fill:none;stroke:var(--accent)" stroke-width="2"/>
  <text x="45" y="206" text-anchor="middle" font-size="17" style="fill:var(--text)">+</text>
  <text x="76" y="205" font-size="14" style="fill:var(--faint)">−</text>
  <rect x="314" y="54" width="92" height="84" rx="10" style="fill:var(--surface);stroke:var(--border)"/>
  <path d="M345 66h20l12 12v34a3 3 0 0 1-3 3h-29a3 3 0 0 1-3-3V69a3 3 0 0 1 3-3z" style="fill:var(--accent-soft);stroke:var(--accent)" stroke-width="1.5"/>
  <text x="360" y="130" text-anchor="middle" font-size="10" font-weight="600" style="fill:var(--text)">python3.12</text>
  <text x="360" y="152" text-anchor="middle" font-size="10" style="fill:var(--muted)">the file from Finder</text>
  <path d="M312 96C296 96 292 118 272 121" fill="none" style="stroke:var(--accent)" stroke-width="2" stroke-dasharray="5 4"/>
  <path d="M276 114l-6 7 8 4" fill="none" style="stroke:var(--accent)" stroke-width="2" stroke-linecap="round"/>
  <circle cx="300" cy="78" r="10" style="fill:var(--accent)"/><text x="300" y="82" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">A</text>
  <circle cx="20" cy="201" r="10" style="fill:var(--accent)"/><text x="20" y="205" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">B</text>
  <text x="312" y="190" font-size="10" font-weight="600" style="fill:var(--accent)">A  drag it in</text>
  <text x="312" y="208" font-size="10" font-weight="600" style="fill:var(--accent)">B  or click +</text>
  <text x="312" y="226" font-size="10" font-weight="600" style="fill:var(--ok)">then turn it on</text>
</svg>`;

function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}
const macOf = (st) => st.collectors.find((c) => c.collector === 'mac') || st.collectors[0];

function previewStatus() {
  const mac = (ok, error) => [{ collector: 'mac', ok, error, message: '', at: new Date().toISOString() }];
  return { local: true, required: true, fake_llm: false, can_open_settings: true, sources: 0, api_key: PREVIEW !== 'key',
    python: '/Users/you/.radar/python/cpython-3.12.15-macos-aarch64-none/bin/python3.12',
    collectors: PREVIEW === 'blocked' ? mac(false, 'no_access') : PREVIEW === 'chats' ? mac(true, '') : [] };
}
const getSetup = () => (PREVIEW ? previewStatus() : api('/api/setup'));

async function runSetupWizard(view) {
  clearTimeout(setupTimer);
  const st = await getSetup();
  if (!st.required) { document.body.classList.remove('setup-mode'); return false; }
  document.body.classList.add('setup-mode');
  const mac = macOf(st);
  const current = !(st.api_key || st.fake_llm) ? 0 : !(mac && mac.ok) ? 1 : 2;   // step 3 stays open until "Finish"
  const again = (ms) => { setupTimer = setTimeout(render, ms); };

  const steps = [
    { title: 'Add your Claude API key', summary: 'Saved and checked', body: () => keyStep() },
    { title: 'Let Chatter Radar read WhatsApp for Mac', summary: 'Connected', body: () => { again(3000); return whatsappStep(st, mac); } },
    { title: 'Choose the chats to watch', summary: '', body: () => chatsStep(st, again) },
  ];
  const list = h('ol', { class: 'wz-list' }, steps.map((s, i) => {
    const kind = i < current ? 'done' : i === current ? 'current' : 'locked';
    return h('li', { class: `wz-step ${kind}` },
      h('span', { class: 'wz-mark', 'aria-label': kind === 'done' ? 'done' : `step ${i + 1}` }, kind === 'done' ? h('span', { html: ICON.check }) : String(i + 1)),
      h('div', { class: 'wz-main' },
        h('div', { class: 'wz-title' }, s.title, kind === 'done' && h('span', { class: 'wz-sum' }, s.summary)),
        kind === 'current' && h('div', { class: 'wz-body' }, s.body()),
        kind === 'locked' && h('div', { class: 'wz-lock' }, 'Finish the step above first')));
  }));
  view.replaceChildren(h('div', { class: 'wizard' },
    h('div', { class: 'wz-head' },
      h('div', { class: 'brand' }, h('span', { html: $('.brand svg').outerHTML }), h('span', { class: 'wordmark' }, h('span', { class: 'lite' }, 'Chatter'), ' Radar')),
      h('h1', {}, 'Let\'s set up Chatter Radar'),
      h('p', { class: 'hint' }, `Step ${current + 1} of 3. This takes about two minutes, and you only do it once.`),
      h('div', { class: 'wz-bar' }, h('i', { style: { width: `${(current / 3) * 100}%` } }))),
    list));
  return true;
}

function keyStep() {
  const key = h('input', { type: 'password', placeholder: 'sk-ant-…', autocomplete: 'off', class: 'wz-input', 'aria-label': 'Claude API key' });
  const err = h('div', { class: 'wz-err', role: 'alert', hidden: true });
  const btn = h('button', { class: 'btn primary', onclick: save }, 'Check and save');
  async function save() {
    err.hidden = true; btn.disabled = true; btn.textContent = 'Checking…';
    try {
      const r = await fetch('/api/setup/api-key', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ key: key.value }) });
      if (r.ok) { render(); return; }
      err.textContent = (await r.json().catch(() => ({}))).detail || 'Could not check the key.';
    } catch { err.textContent = 'Could not reach Chatter Radar. Is it still running?'; }
    err.hidden = false; btn.disabled = false; btn.textContent = 'Check and save';
  }
  key.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
  return h('div', {},
    h('p', { class: 'hint' }, 'Chatter Radar uses Claude to read your messages, so you need your own Anthropic key. About $5 a month covers typical use, and Chatter Radar tells you when the credit runs out.'),
    h('ol', { class: 'wz-how' },
      h('li', {}, 'Sign in at ', h('a', { href: 'https://console.anthropic.com/settings/billing', target: '_blank', rel: 'noopener' }, 'console.anthropic.com'), ' and add $5 of credit (usually enough for a month or more).'),
      h('li', {}, 'Open ', h('a', { href: 'https://console.anthropic.com/settings/keys', target: '_blank', rel: 'noopener' }, 'API keys'), ', press Create Key, and copy it.'),
      h('li', {}, 'Paste it here. It is checked with Anthropic first, and stays on this Mac.')),
    h('div', { class: 'wz-row' }, key, btn), err);
}

function whatsappStep(st, mac) {
  const act = async (path, body) => { try { await api(path, { method: 'POST', body }); } catch { /* api() already showed the error */ } };
  const checkBtn = h('button', { class: 'btn primary', onclick: async () => {
    checkBtn.disabled = true; checkBtn.textContent = 'Checking…';
    await act('/api/setup/check-now', {});
    setTimeout(render, 4000);
  } }, 'I\'ve done it. Check now');
  let status;
  if (mac && mac.error === 'no_whatsapp') status = h('div', { class: 'wz-err' }, 'WhatsApp for Mac wasn\'t found. Install it from the Mac App Store and link it to your phone (WhatsApp → Settings → Linked devices), then press the button.');
  else if (mac && mac.error === 'no_access') status = h('div', { class: 'wz-note' }, 'Not connected yet: macOS is still blocking access. Finish the steps above, then press the button.');
  else if (mac) status = h('div', { class: 'wz-err' }, `The collector hit a problem: ${mac.message || mac.error}. See ~/.radar/logs/collector.log.`);
  else status = h('div', { class: 'wz-note' }, 'Waiting for the collector\'s first check…');
  return h('div', {},
    h('p', { class: 'hint' }, 'Chatter Radar reads the chats WhatsApp for Mac keeps on this computer. macOS protects that data, and has no pop-up for it, so you allow it once by hand:'),
    h('div', { class: 'wz-art', html: FDA_SVG }),
    h('ol', { class: 'wz-how' },
      h('li', {}, st.can_open_settings
        ? h('button', { class: 'btn', onclick: () => act('/api/setup/open-settings', { what: 'fda' }) }, 'Open Full Disk Access settings')
        : h('a', { class: 'btn', href: FDA_URL }, 'Open Full Disk Access settings')),
      h('li', {}, 'Add this file to the list, either by dragging it in from Finder (A) or with the + button (B):',
        h('code', { class: 'pathbox' }, st.python),
        h('div', { class: 'wz-row' },
          st.can_open_settings && h('button', { class: 'btn', onclick: () => act('/api/setup/open-settings', { what: 'file' }) }, 'Show the file in Finder'),
          h('button', { class: 'btn ghost', onclick: () => navigator.clipboard.writeText(st.python).then(() => toast('Path copied')).catch(() => toast('Select the path and copy it')) }, 'Copy path'))),
      h('li', {}, 'Make sure the switch next to it is on. (With +, press ⌘⇧G to paste the path.)')),
    h('div', { class: 'wz-row' }, checkBtn), status);
}

function chatsStep(st, again) {
  const picked = new Map();   // chat name -> source id
  const box = h('div', { class: 'wz-chats' });
  const count = h('span', { class: 'hint' });
  const finish = h('button', { class: 'btn primary', disabled: true, onclick: async () => {
    try { await api('/api/setup/finish', { method: 'POST', body: {} }); } catch { return; }
    document.body.classList.remove('setup-mode'); state.tab = 'tasks'; setTab('tasks');
  } }, 'Finish setup');
  const filter = h('input', { type: 'search', class: 'wz-input', placeholder: 'Search your chats', dir: 'auto', 'aria-label': 'Search chats' });
  let chats = [];
  const refreshFooter = () => {
    finish.disabled = !picked.size;
    count.textContent = picked.size ? `${picked.size} chat${picked.size > 1 ? 's' : ''} selected` : 'Pick at least one chat to continue.';
  };
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const rows = chats.filter((c) => !q || c.name.toLowerCase().includes(q)).slice(0, 200).map((c) => {
      const on = picked.has(c.name);
      return h('div', { class: 'wz-chat' + (on ? ' on' : '') },
        h('span', { dir: 'auto', class: 'wz-chat-name' }, c.name),
        h('button', { class: 'btn' + (on ? ' ok' : ''), onclick: async () => {
          try {
            if (on) { await api(`/api/sources/${picked.get(c.name)}`, { method: 'DELETE' }); picked.delete(c.name); }
            else if (PREVIEW) picked.set(c.name, 0);
            else picked.set(c.name, (await api('/api/sources', { body: { name: c.name, match_pattern: c.name, channel: c.channel, history_days: 7 } })).id);
          } catch { return; }
          draw(); refreshFooter();
        } }, on ? '✓ Watching' : 'Watch'));
    });
    box.replaceChildren(...(rows.length ? rows : [h('div', { class: 'hint wz-empty' }, chats.length ? 'No chat matches.' : 'Chatter Radar is reading your chat list… this takes a few seconds.')]));
  };
  filter.addEventListener('input', draw);
  (async () => {
    chats = PREVIEW ? ['Family', 'Class parents', 'Football club', 'Dana', 'Work team'].map((name) => ({ name, channel: 'whatsapp' })) : await api('/api/chats');
    for (const c of chats) if (c.source) picked.set(c.name, c.source.id);
    draw(); refreshFooter();
    if (!chats.length) again(4000);
  })();
  return h('div', {},
    h('p', { class: 'hint' }, 'Chatter Radar only stores the chats you pick here, and sends only those to Claude. Other chats are never saved. You can change this any time in the Sources tab.'),
    filter, box, h('div', { class: 'wz-row wz-foot' }, finish, count));
}

// Claude can't be reached for a reason only the user can fix: no credit left, or the key was rejected.
function llmProblemBanner(p) {
  const retry = async () => { await api('/api/llm/retry', { method: 'POST', body: {} }); toast('Trying again…'); setTimeout(render, 4000); };
  if (p.kind === 'credit') {
    return h('div', { class: 'banner alert' }, 'Your Anthropic credit has run out, so new messages are not being analyzed. They are kept and will be analyzed once you add credit.',
      h('a', { class: 'btn', href: 'https://console.anthropic.com/settings/billing', target: '_blank', rel: 'noopener' }, 'Add credit'),
      h('button', { class: 'btn primary', onclick: retry }, 'I\'ve added credit, retry'));
  }
  const key = h('input', { type: 'password', placeholder: 'sk-ant-…', autocomplete: 'off', 'aria-label': 'New API key' });
  return h('div', { class: 'banner alert' }, 'Anthropic rejected your API key, so messages are not being analyzed. Create a new key and paste it here:', key,
    h('button', { class: 'btn primary', onclick: async () => {
      try { await api('/api/setup/api-key', { body: { key: key.value } }); await retry(); } catch { /* api() shows the error */ }
    } }, 'Save key'));
}

// After setup: a slim warning only when something later breaks.
async function setupBanner() {
  const st = await api('/api/setup');
  if (st.required) return null;
  if (st.llm_problem) return llmProblemBanner(st.llm_problem);
  const mac = macOf(st);
  const stale = mac && (Date.now() - new Date(mac.at)) > 90 * 60000;
  let msg = null, fix = null;
  if (!st.api_key && !st.fake_llm) msg = 'No Claude API key is set, so Chatter Radar can\'t analyze anything. Set ANTHROPIC_API_KEY on the server.';
  else if (st.local && mac && mac.error === 'no_access') {
    msg = 'macOS stopped Chatter Radar from reading WhatsApp, so no new messages are coming in. Allow the file in Full Disk Access again: ';
    fix = h('button', { class: 'btn', onclick: () => api('/api/setup/open-settings', { body: { what: 'fda' } }) }, 'Open settings');
  } else if (st.local && mac && !mac.ok) msg = `The Mac collector has a problem: ${mac.message || mac.error}. See ~/.radar/logs/collector.log.`;
  else if (st.local && stale) msg = `The Mac collector hasn't checked in since ${ago(mac.at)}. Is the Mac asleep?`;
  return msg ? h('div', { class: 'banner alert' }, msg, fix) : null;
}

async function renderTasks(view) {
  const q = new URLSearchParams({ status: state.taskStatus });
  if (state.taskCat) q.set('category', state.taskCat);
  if (state.taskSource) q.set('source_id', state.taskSource);
  const tasks = await api('/api/tasks?' + q);
  const ins = state.instr || await api('/api/instructions');
  const bar = h('div', { class: 'toolbar' },
    segmented([['open', 'Open'], ['done', 'Done'], ['irrelevant', 'Irrelevant'], ['closed', 'All closed']], state.taskStatus,
      (v) => { state.taskStatus = v; save(); render(); }),
    catSelect(state.taskCat, (v) => { state.taskCat = v; render(); }),
    sourceSelect(state.taskSource, (v) => { state.taskSource = v; render(); }));
  view.replaceChildren(bar);
  const warn = await setupBanner();
  if (warn) view.append(warn);
  if (ins.pending) {
    view.append(h('div', { class: 'banner' }, `The assistant has ${ins.pending} change${ins.pending > 1 ? 's' : ''} to the pipeline waiting for your approval.`,
      h('button', { class: 'btn', onclick: () => setTab('assistant') }, 'Review')));
  } else if (ins.new_feedback >= 3) {
    view.append(h('div', { class: 'banner' }, `You've labeled ${ins.new_feedback} tasks/messages since the last review.`,
      h('button', { class: 'btn', onclick: () => discuss('Review my recent feedback and propose instruction changes that would fix the patterns you find.', true) }, 'Teach the assistant')));
  }

  if (!tasks.length) {
    view.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, state.taskStatus === 'open' ? '✓' : '∅'),
      state.taskStatus === 'open' ? 'Nothing needs your attention.' : 'Nothing here yet.',
      !state.sources.length && h('p', { class: 'hint' }, 'Start by adding a source on the Sources tab.')));
    return;
  }
  if (state.taskStatus === 'open') {
    const order = ['Overdue', 'Today', 'Tomorrow', 'This week', 'Later', 'No date'];
    const groups = Object.groupBy ? Object.groupBy(tasks, bucket) : tasks.reduce((a, t) => ((a[bucket(t)] ||= []).push(t), a), {});
    for (const g of order) if (groups[g]) view.append(h('h2', {}, g), h('div', { class: 'list' }, groups[g].map(taskCard)));
  } else {
    view.append(h('div', { class: 'list' }, tasks.map(taskCard)));
  }
}

function taskCard(t) {
  const c = catByKey(t.category_key);
  const open = t.status === 'open';
  const when = [t.due_date && fmtDate(t.due_date), t.start_time && (t.end_time ? `${t.start_time}–${t.end_time}` : t.start_time)].filter(Boolean).join(' · ');
  const overdue = open && t.due_date && t.due_date < todayISO();
  const card = h('div', { class: 'card task' + (open ? '' : ' closed'), 'data-id': t.id });

  const actions = h('div', { class: 'actions' });
  if (open) {
    put(actions,
      h('button', { class: 'btn ok', onclick: () => act(t, 'done', 'Marked done') }, icon('check'), 'Done'),
      h('button', { class: 'btn', title: 'Open a prefilled Google Calendar event', onclick: () => sendToCalendar(t, card) },
        icon('cal'), t.calendar_sent_at ? 'In calendar ✓' : 'Add to calendar'),
      t.payment_url && h('a', { class: 'btn', href: t.payment_url, target: '_blank', rel: 'noopener' }, icon('pay'), 'Pay'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn ghost', onclick: () => editForm(t, card) }, 'Edit'),
      h('button', { class: 'btn ghost', title: 'Not relevant — teaches the pipeline', onclick: () => irrelevantForm(t, card) }, icon('x'), 'Irrelevant'));
  } else {
    put(actions, h('button', { class: 'btn ghost', onclick: () => act(t, 'reopen', 'Reopened') }, icon('undo'), 'Reopen'));
  }

  const evidence = t.messages.length ? h('details', { class: 'evidence' },
    h('summary', {}, `${t.messages.length === 1 ? 'Message' : t.messages.length + ' messages'} · ${t.messages[0].chat_name}`),
    t.messages.map((m) => h('div', { class: 'quote' }, h('div', { class: 'who' }, `${m.sender || '—'} · ${fmtStamp(m.sent_at)}`), m.body,
      attachmentsView(m.attachments)))) : null;

  put(card,
    h('div', {},
      h('div', { class: 'title', dir: 'auto' }, t.title),
      h('div', { class: 'meta' },
        when && h('span', { class: 'when' + (overdue ? ' overdue' : '') }, when),
        t.location && h('span', { dir: 'auto' }, '📍 ' + t.location),
        t.amount != null && h('span', { class: 'amount' }, money(t.amount, t.currency)),
        h('span', {}, t.source_name || 'Manual'))),
    h('div', { class: 'side' },
      h('span', { class: 'pill', style: { '--c': c.color } }, h('span', { class: 'dot' }), c.name),
      !open && h('span', { class: 'pill status' }, t.status),
      t.origin === 'manual' ? h('span', { class: 'conf' }, 'added by you') : t.origin === 'agent' ? h('span', { class: 'conf' }, 'added by assistant') : (t.confidence != null && t.confidence < 0.5 && h('span', { class: 'conf' }, 'unsure'))),
    t.details && h('div', { class: 'details', dir: 'auto' }, t.details),
    t.status_note && !open && h('div', { class: 'details' }, '— ' + t.status_note),
    evidence, actions);
  card.style.setProperty('--c', c.color);
  return card;
}

async function act(t, action, msg) {
  await api(`/api/tasks/${t.id}/${action}`, { method: 'POST' });
  const inverse = action === 'reopen' ? null : 'reopen';
  toast(msg, inverse && (async () => { await api(`/api/tasks/${t.id}/${inverse}`, { method: 'POST' }); render(); }));
  render();
}

function sendToCalendar(t, card) {
  // Open synchronously inside the click handler (popup blockers). 'noopener' would make window.open return null.
  const w = window.open(t.calendar_url, '_blank');
  if (w) w.opener = null;
  api(`/api/tasks/${t.id}/calendar`, { method: 'POST' }).then(() => {
    const b = [...card.querySelectorAll('button')].find((x) => x.textContent.includes('calendar'));
    if (b) b.lastChild.textContent = 'In calendar ✓';
  });
  if (!w) location.href = t.calendar_url;
}

function irrelevantForm(t, card) {
  if (card.querySelector('.inline-form')) return;
  const note = h('input', { type: 'text', placeholder: 'Why? (optional — e.g. "just chatter", "not my kid")', style: { flex: 1, minWidth: '200px' } });
  const form = h('div', { class: 'inline-form' },
    h('div', { class: 'row' }, note,
      h('button', { class: 'btn primary', onclick: async () => {
        await api(`/api/tasks/${t.id}/irrelevant`, { body: { note: note.value } });
        toast('Marked irrelevant', [
          { label: 'Undo', fn: async () => { await api(`/api/tasks/${t.id}/reopen`, { method: 'POST' }); render(); } },
          { label: 'Teach the assistant', fn: () => discuss(`I marked the task “${t.title}” (#${t.id}) as irrelevant${note.value ? ` — ${note.value}` : ''}. Can you figure out why it was created and propose a rule so this doesn't happen again?`) }]);
        render();
      } }, 'Mark irrelevant'),
      h('button', { class: 'btn ghost', onclick: () => form.remove() }, 'Cancel')));
  card.append(form); note.focus();
  note.addEventListener('keydown', (e) => { if (e.key === 'Enter') form.querySelector('.btn.primary').click(); });
}

function editForm(t, card) {
  if (card.querySelector('.inline-form')) return;
  const title = h('input', { type: 'text', dir: 'auto', value: t.title, style: { flex: 1, minWidth: '200px' } });
  const date = h('input', { type: 'date', value: t.due_date || '' });
  const time = h('input', { type: 'time', value: t.start_time || '' });
  const details = h('textarea', { dir: 'auto', rows: 2, placeholder: 'Details', style: { flex: 1 } }, t.details || '');
  const form = h('div', { class: 'inline-form' },
    h('div', { class: 'row' }, title, date, time),
    h('div', { class: 'row' }, details),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: async () => {
        await api(`/api/tasks/${t.id}`, { method: 'PATCH', body: {
          title: title.value.trim() || t.title, details: details.value.trim() || null,
          due_date: date.value || null, start_time: time.value || null } });
        toast('Task saved');
        render();
      } }, 'Save'),
      h('button', { class: 'btn ghost', onclick: () => form.remove() }, 'Cancel')));
  card.append(form); title.focus();
}

// ---------------------------------------------------------------- messages
async function renderMessages(view, { append = false, beforeId = null } = {}) {
  const q = new URLSearchParams({ limit: '100' });
  if (state.msgSource) q.set('source_id', state.msgSource);
  if (state.msgQuery) q.set('q', state.msgQuery);
  if (state.msgOnly) q.set('only', state.msgOnly);
  if (beforeId) q.set('before_id', beforeId);
  const msgs = await api('/api/messages?' + q);

  if (!append) {
    const search = h('input', { type: 'search', placeholder: 'Search messages', value: state.msgQuery, style: { flex: 1, minWidth: '160px' } });
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { state.msgQuery = search.value; render(); } });
    view.replaceChildren(
      h('div', { class: 'toolbar' },
        sourceSelect(state.msgSource, (v) => { state.msgSource = v; save(); render(); }),
        segmented([['', 'All'], ['untasked', 'No task'], ['pending', 'Not analyzed yet']], state.msgOnly, (v) => { state.msgOnly = v; render(); }),
        search),
      h('p', { class: 'hint' }, 'Every message from your sources. If one should have become a task, flag it — flagged messages are fed back to the model as examples.'),
      h('div', { class: 'list', id: 'msg-list' }));
  }
  const list = $('#msg-list');
  if (!msgs.length && !append) { list.append(h('div', { class: 'empty' }, 'No messages yet.')); return; }
  let lastDay = list.dataset.lastDay || '';
  for (const m of msgs) {
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: TZ }).format(new Date(m.sent_at));
    if (day !== lastDay) { list.append(h('div', { class: 'day-sep' }, fmtDate(day))); lastDay = day; }
    list.append(messageCard(m));
  }
  list.dataset.lastDay = lastDay;
  $('#more-btn')?.remove();
  if (msgs.length === 100) {
    view.append(h('div', { style: { textAlign: 'center', marginTop: '12px' } },
      h('button', { class: 'btn', id: 'more-btn', onclick: () => renderMessages(view, { append: true, beforeId: msgs[msgs.length - 1].id }) }, 'Load older')));
  }
}

function messageCard(m) {
  const card = h('div', { class: 'card msg' + (m.missed ? ' flagged' : '') });
  const foot = h('div', { class: 'msg-foot' },
    m.tasks.map((t) => h('span', { class: 'pill', style: { '--c': t.category_color || '#6b7280' }, title: t.status },
      h('span', { class: 'dot' }), t.title.length > 40 ? t.title.slice(0, 40) + '…' : t.title, t.status !== 'open' ? ` (${t.status})` : '')),
    !m.analyzed_at && h('span', { class: 'badge' }, 'pending analysis'),
    m.missed && h('span', { class: 'pill', style: { '--c': 'var(--warn)' } }, icon('flag'), 'Missed: ' + (catByKey(m.missed.category_key).name || '?')),
    m.missed && h('button', { class: 'btn ghost', onclick: async () => { await api(`/api/feedback/${m.missed.id}`, { method: 'DELETE' }); toast('Flag removed'); render(); } }, 'Unflag'),
    h('span', { class: 'spacer' }),
    !m.missed && h('button', { class: 'btn ghost', onclick: () => missedForm(m, card) }, icon('flag'), 'Should be a task'));
  put(card,
    h('div', { class: 'msg-head' },
      h('span', { class: 'sender', dir: 'auto' }, m.sender || m.chat_name),
      h('span', {}, fmtStamp(m.sent_at)),
      !state.msgSource && h('span', {}, '· ' + m.source_name),
      m.collectors.map((c) => h('span', { class: 'badge' }, c))),
    h('div', { class: 'msg-body', dir: 'auto' }, m.body),
    attachmentsView(m.attachments),
    foot);
  return card;
}

function missedForm(m, card) {
  if (card.querySelector('.inline-form')) return;
  let cat = state.categories.find((c) => c.enabled)?.key || '';
  const note = h('input', { type: 'text', placeholder: 'What should it have caught? (optional)', style: { flex: 1, minWidth: '200px' } });
  const create = h('input', { type: 'checkbox', checked: true });
  const title = h('input', { type: 'text', placeholder: 'Task title', value: m.body.slice(0, 80), dir: 'auto', style: { flex: 1, minWidth: '200px' } });
  const date = h('input', { type: 'date' });
  const time = h('input', { type: 'time' });
  const taskRow = h('div', { class: 'row' }, title, date, time);
  create.addEventListener('change', () => { taskRow.hidden = !create.checked; });
  const form = h('div', { class: 'inline-form' },
    h('div', { class: 'row' }, h('label', {}, 'Category', catSelect(cat, (v) => { cat = v; }, { all: false })), h('label', { style: { flex: 1 } }, 'Note', note)),
    h('label', { class: 'check' }, create, 'Also add it to my task list'),
    taskRow,
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: async () => {
        await api(`/api/messages/${m.id}/missed`, { body: { category_key: cat, note: note.value, create_task: create.checked,
          title: title.value, due_date: date.value || null, start_time: time.value || null } });
        toast(create.checked ? 'Flagged and added to tasks' : 'Flagged as missed', [
          { label: 'Teach the assistant', fn: () => discuss(`Message #${m.id} from ${m.source_name} (“${m.body.slice(0, 120)}”) should have become a ${catByKey(cat).name} task but didn't${note.value ? ` — ${note.value}` : ''}. Can you work out why and propose a fix?`) }]);
        render();
      } }, 'Save'),
      h('button', { class: 'btn ghost', onclick: () => form.remove() }, 'Cancel')));
  card.append(form);
}

// ---------------------------------------------------------------- sources
async function renderSources(view) {
  await loadMeta();
  const chats = await api('/api/chats');
  const rows = chats.map((c) => h('tr', {},
    h('td', { dir: 'auto' }, c.name),
    h('td', { class: 'num' }, c.msg_count),
    h('td', {}, c.seen_by.join(', ')),
    h('td', {}, fmtStamp(c.last_seen)),
    h('td', {}, c.source ? h('span', { class: 'hint' }, '→ ' + c.source.name)
      : h('button', { class: 'btn', onclick: () => sourceForm({ name: c.name, match_pattern: c.name, channel: c.channel, context: '', enabled: 1 }, view) }, 'Add as source'))));
  const filter = h('input', { type: 'search', placeholder: `Filter ${chats.length} chats`, dir: 'auto', style: { width: '100%', marginBottom: '8px' },
    oninput: () => {
      const q = filter.value.trim().toLowerCase();
      chats.forEach((c, i) => { rows[i].style.display = !q || c.name.toLowerCase().includes(q) ? '' : 'none'; });
    } });
  view.replaceChildren(
    h('div', { class: 'toolbar' }, h('h2', { style: { margin: 0 } }, 'Sources'), h('span', { class: 'spacer' }),
      h('button', { class: 'btn primary', onclick: () => sourceForm(null, view) }, '+ Add source')),
    h('div', { id: 'src-form' }),
    state.sources.length
      ? h('div', { class: 'list' }, state.sources.map((s) => sourceCard(s, view)))
      : h('div', { class: 'empty' }, 'No sources yet. Add one, or pick a chat below once a collector has reported its chats.'),
    h('h2', {}, 'Chats seen by your collectors'),
    h('p', { class: 'hint' }, 'Only names and counts are kept for chats that aren\'t a source. Add a chat as a source to start storing and analyzing its messages.'),
    ...(chats.length ? [filter, h('div', { class: 'card table-card' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Chat'), h('th', { class: 'num' }, 'Msgs'), h('th', {}, 'Seen by'), h('th', {}, 'Last'), h('th', {}, ''))),
      h('tbody', {}, rows)))]
      : [h('p', { class: 'hint' }, 'No chats reported yet.')]));
}

function sourceCard(s, view) {
  return h('div', { class: 'card src' },
    h('div', { class: 'src-head' },
      h('span', { class: 'name', dir: 'auto' }, s.name),
      !s.enabled && h('span', { class: 'pill status' }, 'paused'),
      s.is_direct ? h('span', { class: 'badge' }, '1:1 chat') : h('span', { class: 'badge' }, 'group'),
      h('span', { class: 'badge' }, s.channel),
      h('span', { class: 'spacer' }),
      s.history_count > 0 && h('button', { class: 'btn', title: 'Messages from the last 7 days that were stored as history but never analyzed',
        onclick: async () => { const r = await api(`/api/sources/${s.id}/analyze-history`, { method: 'POST', body: { days: 7 } });
          toast(`Analyzing ${r.requeued} older message${r.requeued === 1 ? '' : 's'}…`); setTimeout(render, 6000); } }, `Analyze last 7 days (${s.history_count})`),
      s.pending_count > 0 && h('button', { class: 'btn', onclick: async () => { const r = await api(`/api/sources/${s.id}/analyze`, { method: 'POST' });
        toast(r.error ? 'Analysis failed: ' + r.error.slice(0, 80) : `Analyzed — ${r.tasks_created} new task(s)`); render(); } }, 'Analyze now'),
      h('button', { class: 'btn ghost', onclick: () => sourceForm(s, view) }, 'Edit')),
    h('div', { class: 'kv' },
      h('span', {}, 'matches “', h('span', { dir: 'auto' }, s.match_pattern), '”'),
      h('span', {}, `${s.message_count} messages`),
      s.pending_count > 0 && h('span', {}, `${s.pending_count} waiting`),
      h('span', {}, `${s.open_tasks} open tasks`),
      s.last_message_at && h('span', {}, 'last ' + fmtStamp(s.last_message_at))),
    s.context && h('div', { class: 'ctx', dir: 'auto' }, s.context));
}

function sourceForm(s, view) {
  const holder = $('#src-form');
  const f = { name: '', match_pattern: '', channel: 'whatsapp', context: '', enabled: 1, is_direct: 0, ...(s || {}) };
  const name = h('input', { type: 'text', value: f.name, dir: 'auto', placeholder: 'e.g. Class 4B parents' });
  const pat = h('input', { type: 'text', value: f.match_pattern, dir: 'auto', placeholder: 'Part of the chat name' });
  const channel = h('input', { type: 'text', value: f.channel, style: { width: '120px' } });
  const ctx = h('textarea', { dir: 'auto', placeholder: 'Tell the model about this source: who\'s who, what matters, what to ignore.\ne.g. "My child\'s class parents group. The teacher is Ms. Green and the class rep is Dana. Ignore birthday wishes."' }, f.context);
  const enabled = h('input', { type: 'checkbox', checked: !!f.enabled });
  const direct = h('input', { type: 'checkbox', checked: !!f.is_direct });
  const history = h('input', { type: 'checkbox', checked: true });
  const form = h('div', { class: 'card inline-form', style: { marginBottom: '12px' } },
    h('div', { class: 'row' }, h('label', { style: { flex: 1 } }, 'Name', name), h('label', { style: { flex: 1 } }, 'Chat name contains', pat), h('label', {}, 'Channel', channel)),
    h('label', {}, 'Context for the model', ctx),
    h('div', { class: 'row' },
      h('label', { class: 'check' }, direct, 'One-on-one chat (e.g. a therapist)'),
      h('label', { class: 'check' }, enabled, 'Enabled')),
    !(s && s.id) && h('label', { class: 'check' }, history, 'Also look for tasks in the last 7 days of this chat (recommended)'),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', onclick: async () => {
        const body = { name: name.value, match_pattern: pat.value, channel: channel.value, context: ctx.value, enabled: enabled.checked, is_direct: direct.checked,
          history_days: !(s && s.id) && history.checked ? 7 : 0 };
        if (s && s.id) await api(`/api/sources/${s.id}`, { method: 'PUT', body }); else await api('/api/sources', { body });
        toast('Source saved'); render();
      } }, 'Save'),
      h('button', { class: 'btn ghost', onclick: () => holder.replaceChildren() }, 'Cancel'),
      h('span', { class: 'spacer' }),
      s && s.id && h('button', { class: 'btn danger', onclick: async () => {
        if (!confirm(`Delete "${s.name}" and all its stored messages?`)) return;
        await api(`/api/sources/${s.id}`, { method: 'DELETE' }); toast('Source deleted'); render();
      } }, 'Delete')));
  holder.replaceChildren(form);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  name.focus();
}

// ---------------------------------------------------------------- pipeline / settings
async function renderSettings(view) {
  await loadMeta();
  const [stats, feedback, batches, info] = await Promise.all([api('/api/stats'), api('/api/feedback'), api('/api/batches?limit=20'), api('/api/info')]);
  const pct = (a, b) => b ? Math.round((100 * a) / b) + '%' : '—';
  view.replaceChildren(...[
    h('h2', {}, 'How it\'s doing'),
    h('div', { class: 'card table-card' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'Category'), h('th', { class: 'num' }, 'Found'), h('th', { class: 'num' }, 'Done'),
        h('th', { class: 'num' }, 'Irrelevant'), h('th', { class: 'num' }, 'Missed'), h('th', { class: 'num', title: '1 − irrelevant / found' }, 'Precision'))),
      h('tbody', {}, stats.categories.map((c) => h('tr', {},
        h('td', {}, h('span', { class: 'pill', style: { '--c': c.color } }, h('span', { class: 'dot' }), c.name)),
        h('td', { class: 'num' }, c.created), h('td', { class: 'num' }, c.done), h('td', { class: 'num' }, c.irrelevant),
        h('td', { class: 'num' }, c.missed), h('td', { class: 'num' }, pct(c.created - c.irrelevant, c.created))))))),
    h('p', { class: 'hint' }, `${stats.totals.messages} messages stored · ${stats.totals.pending} waiting · model ${info.model}${info.fake_llm ? ' (FAKE mode)' : ''} · ` +
      `${(stats.totals.input_tokens / 1000).toFixed(1)}k in / ${(stats.totals.output_tokens / 1000).toFixed(1)}k out tokens` +
      (stats.totals.failed_batches ? ` · ${stats.totals.failed_batches} failed runs` : '')),

    scheduleSection(info, stats),

    ...(await rulesSection()),

    h('h2', {}, 'What to watch for'),
    h('p', { class: 'hint' }, 'Each category\'s description is given to the model verbatim — be specific about what counts and what doesn\'t.'),
    h('div', { class: 'list' }, state.categories.map(categoryEditor), categoryEditor(null)),

    h('details', { class: 'advanced' },
      h('summary', {}, 'Advanced'),
      h('p', { class: 'hint' }, 'For troubleshooting and for running your own server. Nothing here needs your attention in normal use.'),
    h('h2', {}, 'Your feedback'),
    h('div', { class: 'toolbar' }, h('span', { class: 'hint' }, `${feedback.length} labels — the latest are included in every prompt as examples.`), h('span', { class: 'spacer' }),
      h('a', { class: 'btn', href: '/api/feedback/export.jsonl' }, 'Export as JSONL')),
    feedback.length ? h('div', { class: 'card table-card' }, h('table', {}, h('tbody', {}, feedback.map((f) => h('tr', {},
      h('td', {}, h('span', { class: 'pill', style: { '--c': f.kind === 'missed' ? 'var(--warn)' : 'var(--muted)' } }, f.kind)),
      h('td', { dir: 'auto' }, f.kind === 'missed' ? f.snapshot.body : f.snapshot.title, f.note && h('div', { class: 'hint' }, '“' + f.note + '”')),
      h('td', {}, f.category_key ? catByKey(f.category_key).name : ''),
      h('td', {}, f.source_name || ''),
      h('td', {}, h('button', { class: 'btn ghost', onclick: async () => { await api(`/api/feedback/${f.id}`, { method: 'DELETE' }); render(); } }, 'Remove'))))))) : h('p', { class: 'hint' }, 'No feedback yet.'),

    h('h2', {}, 'Recent analysis runs'),
    batches.length ? h('div', { class: 'list' }, batches.map((b) => h('details', { class: 'card', style: { padding: '10px 14px' } },
      h('summary', { style: { cursor: 'pointer' } }, `${fmtStamp(b.created_at)} · ${b.source_name || '?'} · msgs #${b.first_msg_id}–${b.last_msg_id} · `,
        b.status === 'ok' ? `${b.tasks_created} task(s)` : h('span', { style: { color: 'var(--danger)' } }, b.status), b.input_tokens ? ` · ${b.input_tokens}+${b.output_tokens} tok` : ''),
      b.error && h('pre', { class: 'code' }, b.error),
      h('div', { class: 'hint' }, 'Prompt'), h('pre', { class: 'code' }, (() => { try { const r = JSON.parse(b.request_json); return r.system + '\n\n────────\n\n' + r.user; } catch { return b.request_json; } })()),
      b.response_json && [h('div', { class: 'hint' }, 'Response'), h('pre', { class: 'code' }, JSON.stringify(JSON.parse(b.response_json), null, 2))])))
      : h('p', { class: 'hint' }, 'No runs yet.'),

    info.local ? null : [h('h2', {}, 'Connecting'),
    h('div', { class: 'card', style: { padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: '8px' } },
      h('div', {}, 'Collectors POST to: ', h('span', { class: 'mono' }, info.ingest_url)),
      h('div', { class: 'hint' }, `Header X-Token: <your RADAR_INGEST_TOKEN>${info.token_set ? '' : ' (not set — anyone can post!)'}`),
      h('div', {}, 'Claude / ChatGPT connector: ', info.mcp_enabled ? h('span', { class: 'mono' }, info.mcp_url) : h('span', { class: 'hint' }, 'disabled — set RADAR_MCP_TOKEN (and RADAR_PUBLIC_URL)')),
      info.mcp_enabled && h('div', { class: 'hint' }, 'Claude: Settings → Connectors → Add custom connector, header Authorization: Bearer <RADAR_MCP_TOKEN>. ChatGPT (web, developer mode): use the URL with ?key=<RADAR_MCP_TOKEN>.'))],
    ),
    info.local && h('div', { class: 'hint', style: { marginTop: '24px' } }, 'To stop and remove Chatter Radar, open Terminal and run ', h('code', {}, '~/.radar/bin/radar uninstall'),
      ' (add --purge to delete your data too). It removes the background jobs and the program.'),
    h('div', { style: { marginTop: '24px' } }, h('button', { class: 'btn ghost', onclick: async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; } }, 'Sign out'))].filter(Boolean));
}

function analyzeNowRow(pending) {
  const label = h('span', { class: 'hint' });
  const btn = h('button', { class: 'btn' }, 'Analyze now');
  const show = (n, running) => {
    label.textContent = running ? 'Analyzing…' : n ? `${n} message${n > 1 ? 's' : ''} waiting for analysis` : 'Nothing is waiting for analysis';
    btn.disabled = running || !n; btn.textContent = running ? 'Analyzing…' : 'Analyze now';
  };
  async function poll() {
    for (let i = 0; i < 100 && document.body.contains(btn); i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const s = await api('/api/analyze');
      show(s.pending, s.running);
      if (!s.running) { toast('Analysis finished'); render(); return; }
    }
  }
  btn.addEventListener('click', async () => {
    try { await api('/api/analyze', { method: 'POST', body: {} }); } catch { return; }
    show(pending, true); poll();
  });
  show(pending, false);
  return h('div', { class: 'toolbar', style: { marginBottom: '6px' } }, label, h('span', { class: 'spacer' }), btn);
}

function scheduleSection(info, stats) {
  const analyzeRow = analyzeNowRow(stats.totals.pending);
  let times = info.schedule && info.schedule.length ? [...info.schedule] : ['07:00', '12:00', '16:00', '20:00'];
  let mode = info.schedule && info.schedule.length ? 'scheduled' : 'realtime';
  const box = h('div', { class: 'card', style: { padding: '12px 14px' } });
  const save = async () => {
    try {
      const r = await api('/api/schedule', { body: mode === 'realtime' ? { mode } : { mode, times } });
      info.schedule = r.schedule; info.next_run = r.next_run;
      toast(mode === 'realtime' ? 'Analyzing right after messages arrive' : 'Saved. Chatter Radar will analyze at the times you chose');
      draw();
    } catch { /* api() already showed the error */ }
  };
  function draw() {
    const scheduled = mode === 'scheduled';
    const radio = (value, title, hint) => h('label', { class: 'sched-opt' },
      h('input', { type: 'radio', name: 'sched-mode', checked: mode === value, onchange: () => { mode = value; if (value === 'realtime') save(); else draw(); } }),
      h('span', {}, h('strong', {}, title), h('span', { class: 'hint', style: { display: 'block' } }, hint)));
    box.replaceChildren(...[
      analyzeRow,
      radio('scheduled', 'A few times a day (recommended)', 'Cheapest. Chatter Radar reads everything new at the times below. Messages that look urgent (today, cancelled, early pickup…) are analyzed within a couple of minutes anyway.'),
      scheduled && h('div', { class: 'sched-times' },
        times.map((t, i) => h('span', { class: 'sched-time' },
          h('input', { type: 'time', value: t, 'aria-label': `Time ${i + 1}`, onchange: (e) => { times[i] = e.target.value; } }),
          times.length > 1 && h('button', { class: 'btn ghost', title: 'Remove this time', 'aria-label': 'Remove this time', onclick: () => { times.splice(i, 1); draw(); } }, '×'))),
        times.length < 12 && h('button', { class: 'btn', onclick: () => { times.push('18:00'); draw(); } }, '+ Add a time'),
        h('button', { class: 'btn primary', onclick: save }, 'Save times')),
      scheduled && h('div', { class: 'hint', style: { margin: '0 0 8px 26px' } }, `Times are in ${info.timezone}.` + (info.next_run ? ` Next run: ${fmtStamp(info.next_run)}.` : '')),
      radio('realtime', 'Right after messages arrive', `More responsive, but costs about 3 times as much. Chatter Radar analyzes a chat about ${Math.max(1, Math.round(info.debounce_seconds / 60))} minutes after it goes quiet.`)].filter(Boolean));
  }
  draw();
  return h('div', {}, h('h2', {}, 'When messages are analyzed'), box);
}

function categoryEditor(c) {
  const isNew = !c;
  c = c || { name: '', description: '', color: '#7c3aed', enabled: 1, sort: 100 };
  const name = h('input', { type: 'text', value: c.name, placeholder: isNew ? 'New category name' : '' });
  const color = h('input', { type: 'color', value: c.color });
  const enabled = h('input', { type: 'checkbox', checked: !!c.enabled });
  const desc = h('textarea', { dir: 'auto', placeholder: 'What counts, what doesn\'t — this goes straight into the prompt.' }, c.description);
  const saveBtn = h('button', { class: 'btn' + (isNew ? ' primary' : ''), onclick: async () => {
    const body = { name: name.value, description: desc.value, color: color.value, enabled: enabled.checked, sort: c.sort };
    if (isNew) await api('/api/categories', { body }); else await api(`/api/categories/${c.id}`, { method: 'PUT', body });
    toast('Category saved'); render();
  } }, isNew ? 'Add category' : 'Save');
  return h('div', { class: 'card cat-edit' }, color, name,
    h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, !isNew && h('label', { class: 'check hint', style: { display: 'flex', gap: '4px' } }, enabled, 'on'), saveBtn),
    desc);
}

// ---------------------------------------------------------------- boot
(async () => {
  await loadMeta();
  const initial = location.hash.slice(1) || state.tab;
  setTab(['tasks', 'messages', 'sources', 'settings'].includes(initial) ? initial : 'tasks');
  setInterval(() => { if (state.tab === 'tasks' && !document.querySelector('.inline-form') && document.visibilityState === 'visible') render(); }, 60000);
})();


// ---------------------------------------------------------------- attachments
function attachmentsView(atts) {
  if (!atts || !atts.length) return null;
  return h('div', { class: 'atts' }, atts.map((a) => {
    if (a.kind === 'image') return h('a', { href: a.url, target: '_blank', class: 'att-img' }, h('img', { src: a.url, alt: a.filename || 'image', loading: 'lazy' }));
    if (a.kind === 'voice') return h('div', { class: 'att-voice' }, h('audio', { controls: true, preload: 'none', src: a.url }),
      a.transcript ? h('div', { class: 'hint', dir: 'auto' }, '“' + a.transcript + '”') : h('div', { class: 'hint' }, 'not transcribed'));
    return h('a', { class: 'btn att-file', href: a.url, target: '_blank' }, '📄 ' + (a.filename || a.kind));
  }));
}

// ---------------------------------------------------------------- assistant
function md(text) {
  // tiny, safe markdown: escape first, then **bold**, `code`, bullet lines, paragraphs
  const esc = text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const lines = esc.split('\n');
  let html = '', inList = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
    const m = line.match(/^\s*(?:[-•*]|\d+\.)\s+(.*)$/);
    if (m) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${m[1]}</li>`; continue; }
    if (inList) { html += '</ul>'; inList = false; }
    html += line.trim() ? `<p>${line}</p>` : '';
  }
  if (inList) html += '</ul>';
  return html;
}

function discuss(text, send = false) {
  state.draft = text; state.autoSend = send;
  setTab('assistant');
}

async function renderAssistant(view) {
  const convs = await api('/api/conversations');
  if (!state.convId || !convs.find((c) => c.id === state.convId)) state.convId = convs[0]?.id || null;
  const conv = state.convId ? await api(`/api/conversations/${state.convId}`) : { items: [] };

  const pick = h('select', { onchange: (e) => { state.convId = Number(e.target.value) || null; save(); render(); } },
    convs.length ? convs.map((c) => h('option', { value: c.id, selected: c.id === state.convId }, c.title || 'New conversation')) : h('option', {}, 'No conversations yet'));
  const stream = h('div', { class: 'chat-stream', id: 'chat-stream' });
  const input = h('textarea', { id: 'chat-input', dir: 'auto', rows: 2, placeholder: 'Ask about your tasks and messages, or tell me what the pipeline gets wrong…' });
  const sendBtn = h('button', { class: 'btn primary' }, 'Send');
  const composer = h('div', { class: 'composer' }, input, sendBtn);

  const send = async () => {
    const text = input.value.trim();
    if (!text) return;
    const isNew = !state.convId;
    if (isNew) { state.convId = (await api('/api/conversations', { body: {} })).id; save(); }
    input.value = ''; input.disabled = sendBtn.disabled = true;
    stream.append(chatItem({ type: 'user', text }), h('div', { class: 'chat-typing' }, h('span'), h('span'), h('span')));
    stream.lastChild.scrollIntoView({ block: 'end', behavior: 'smooth' });
    try {
      const out = await api(`/api/conversations/${state.convId}/messages`, { body: { text } });
      drawItems(stream, out.items);
      refreshCount();
      if (isNew) pick.replaceChildren(h('option', { value: state.convId, selected: true }, text.slice(0, 60)));
    } finally { input.disabled = sendBtn.disabled = false; input.focus(); }
  };
  sendBtn.onclick = send;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

  const pending = await api('/api/proposals?status=pending');
  const shownIds = new Set(conv.items.filter((i) => i.type === 'proposal').map((i) => i.proposal_id));
  const orphanPending = pending.filter((p) => !shownIds.has(p.id));

  put(view.replaceChildren() || view,
    h('div', { class: 'toolbar' }, pick,
      h('button', { class: 'btn', onclick: async () => { state.convId = (await api('/api/conversations', { body: {} })).id; save(); render(); } }, '+ New chat'),
      h('span', { class: 'spacer' }),
      state.convId && h('button', { class: 'btn ghost', onclick: async () => { if (!confirm('Delete this conversation?')) return; await api(`/api/conversations/${state.convId}`, { method: 'DELETE' }); state.convId = null; render(); } }, 'Delete')),
    orphanPending.length ? h('div', { class: 'list', style: { marginBottom: '12px' } }, orphanPending.map((p) => proposalCard(p))) : null,
    stream, composer);
  drawItems(stream, conv.items);
  if (!conv.items.length) {
    stream.append(h('div', { class: 'chat-empty' },
      h('div', { class: 'big' }, '💬'),
      h('p', {}, 'I can answer questions about your messages and tasks, and help the pipeline learn from your feedback. Changes to its instructions always wait for your approval.'),
      h('div', { class: 'suggestions' }, [
        'What do I need to do tomorrow?',
        'Review my recent feedback and propose instruction changes.',
        'Which payment requests are still open?',
        'What changed in my kids\' groups this week?',
      ].map((q) => h('button', { class: 'btn', onclick: () => { input.value = q; send(); } }, q)))));
  }
  if (state.draft) {
    input.value = state.draft; state.draft = null;
    if (state.autoSend) { state.autoSend = false; send(); } else input.focus();
  }
}

function drawItems(stream, items) {
  stream.replaceChildren(...items.map(chatItem).filter(Boolean));
  const last = stream.lastElementChild;
  if (last) last.scrollIntoView({ block: 'end' });
}

function chatItem(it) {
  if (it.type === 'user') return h('div', { class: 'bubble user', dir: 'auto' }, it.text);
  if (it.type === 'assistant') return h('div', { class: 'bubble bot', dir: 'auto', html: md(it.text) });
  if (it.type === 'tool') return h('div', { class: 'tool-step' }, '· ' + it.label + (it.hint ? ` “${String(it.hint).slice(0, 40)}”` : ''));
  if (it.type === 'tool_error') return h('div', { class: 'tool-step err' }, '· ' + it.text);
  if (it.type === 'event') return h('div', { class: 'tool-step' }, '· ' + it.text);
  if (it.type === 'proposal') {
    const holder = h('div', { class: 'proposal-holder' }, h('div', { class: 'hint' }, 'Loading proposal…'));
    api(`/api/proposals/${it.proposal_id}`).then((p) => holder.replaceChildren(proposalCard(p)));
    return holder;
  }
  return null;
}

function replayTable(rep) {
  const v = rep && rep.variants;
  if (!v || !v.candidate) return null;
  const rows = [['fp', 'False positives avoided'], ['fn', 'Missed messages caught'], ['tp', 'Confirmed tasks kept']]
    .filter(([k]) => v.candidate[k].total);
  if (!rows.length) return null;
  const cell = (x) => x ? `${x.ok}/${x.total}` : '—';
  const cls = (c, b) => !b ? '' : c.ok > b.ok ? 'better' : c.ok < b.ok ? 'worse' : '';
  return h('table', { class: 'replay' },
    h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', { class: 'num' }, 'Now'), h('th', { class: 'num' }, 'With change'))),
    h('tbody', {}, rows.map(([k, label]) => h('tr', {}, h('td', {}, label),
      h('td', { class: 'num' }, cell(v.baseline && v.baseline[k])),
      h('td', { class: 'num ' + cls(v.candidate[k], v.baseline && v.baseline[k]) }, cell(v.candidate[k])))),
      h('tr', {}, h('td', {}, 'Unlabeled extra tasks'), h('td', { class: 'num' }, v.baseline ? v.baseline.extra_tasks : '—'),
        h('td', { class: 'num' }, v.candidate.extra_tasks))));
}

function proposalCard(p) {
  const status = { pending: 'Waiting for your approval', approved: 'Approved — live', rejected: 'Rejected', superseded: 'Superseded' }[p.status];
  const card = h('div', { class: 'card proposal ' + p.status });
  const details = (p.replay?.details || []).filter((d) => d.kind === 'extra' || !d.candidate_ok || d.candidate_ok !== d.baseline_ok);
  const actions = p.status === 'pending' ? h('div', { class: 'row' },
    h('button', { class: 'btn primary', onclick: async () => {
      const np = await api(`/api/proposals/${p.id}/approve`, { method: 'POST' });
      card.replaceWith(proposalCard(np)); toast('Approved — the extractor uses it from the next batch'); refreshCount();
    } }, icon('check'), 'Approve'),
    h('button', { class: 'btn', onclick: () => {
      if (card.querySelector('.reject-row')) return;
      const why = h('input', { type: 'text', placeholder: 'Why not? (helps the assistant revise)', style: { flex: 1, minWidth: '200px' } });
      card.append(h('div', { class: 'row reject-row' }, why, h('button', { class: 'btn danger', onclick: async () => {
        const np = await api(`/api/proposals/${p.id}/reject`, { body: { note: why.value } });
        card.replaceWith(proposalCard(np)); refreshCount();
      } }, 'Reject')));
      why.focus();
    } }, 'Reject…')) : null;
  put(card,
    h('div', { class: 'proposal-head' }, h('strong', {}, `Proposed change #${p.id}`), h('span', { class: 'pill status ' + p.status }, status)),
    h('ul', { class: 'ops' }, p.lines.map((l) => h('li', { dir: 'auto' }, l))),
    p.rationale && h('div', { class: 'hint', dir: 'auto' }, 'Why: ' + p.rationale),
    h('div', { class: 'replay-box' },
      h('div', { class: 'replay-title' }, 'Tested on your labeled history'),
      replayTable(p.replay) || h('div', { class: 'hint' }, p.replay_summary || 'Not tested.'),
      details.length ? h('details', {}, h('summary', { class: 'hint' }, `${details.length} example${details.length > 1 ? 's' : ''} to look at`),
        h('ul', { class: 'ops' }, details.map((d) => h('li', { dir: 'auto' },
          d.kind === 'extra' ? `New task (unlabeled): [${d.category}] ${d.title}`
            : `${{ fp: 'False positive', fn: 'Missed', tp: 'Confirmed' }[d.kind]}: ${d.title || ''} — ${d.candidate_ok ? 'OK with change' : 'still wrong'}${d.baseline_ok === null ? '' : d.baseline_ok ? ' (OK now)' : ' (wrong now)'}`)))) : null),
    p.decided_note && h('div', { class: 'hint' }, 'Your note: ' + p.decided_note),
    actions);
  return card;
}

// ---------------------------------------------------------------- learned rules (Pipeline tab)
async function rulesSection() {
  const ins = await api('/api/instructions');
  const sources = state.sources;
  const groups = [{ id: null, name: 'All sources' }, ...sources.map((s) => ({ id: s.id, name: s.name }))]
    .map((g) => ({ ...g, rules: ins.rules.filter((r) => (r.source_id ?? null) === g.id) })).filter((g) => g.rules.length);
  const ruleRow = (r) => {
    const text = h('span', { dir: 'auto', class: 'rule-text' }, r.text);
    const row = h('li', { class: 'rule' }, text,
      h('span', { class: 'rule-actions' },
        h('button', { class: 'btn ghost', onclick: () => {
          const inp = h('textarea', { dir: 'auto', rows: 2 }, r.text);
          row.replaceChildren(inp, h('span', { class: 'rule-actions' },
            h('button', { class: 'btn primary', onclick: async () => { await api(`/api/rules/${r.id}`, { method: 'PUT', body: { text: inp.value } }); toast('Rule saved'); render(); } }, 'Save'),
            h('button', { class: 'btn ghost', onclick: () => render() }, 'Cancel')));
          inp.focus();
        } }, 'Edit'),
        h('button', { class: 'btn ghost', onclick: async () => { await api(`/api/rules/${r.id}`, { method: 'DELETE' }); toast('Rule removed'); render(); } }, 'Remove')));
    return row;
  };
  const newText = h('input', { type: 'text', dir: 'auto', placeholder: 'Write a rule yourself, e.g. “Birthday wishes are never tasks”', style: { flex: 1, minWidth: '220px' } });
  const newScope = sourceSelect('', () => {}, 'All sources');
  const versions = ins.versions;
  return [
    h('h2', {}, `Learned rules (${ins.rules.length}/${ins.max_rules})`),
    h('p', { class: 'hint' }, 'The extractor follows these on top of the category descriptions. The assistant proposes them from your feedback; you approve. You can also edit them directly.'),
    groups.length ? h('div', { class: 'card rules-card' }, groups.map((g) => [h('div', { class: 'rule-group' }, g.name), h('ul', { class: 'rules' }, g.rules.map(ruleRow))]))
      : h('p', { class: 'hint' }, 'No rules yet.'),
    h('div', { class: 'toolbar', style: { marginTop: '8px' } }, newScope, newText,
      h('button', { class: 'btn', onclick: async () => {
        if (!newText.value.trim()) return;
        await api('/api/rules', { body: { text: newText.value, source_id: newScope.value ? Number(newScope.value) : null } });
        toast('Rule added'); render();
      } }, 'Add rule'),
      h('button', { class: 'btn', onclick: () => discuss('Review my recent feedback and propose instruction changes that would fix the patterns you find.', true) }, 'Ask assistant to review')),
    h('details', { class: 'card', style: { padding: '10px 14px', marginTop: '8px' } },
      h('summary', { style: { cursor: 'pointer' } }, `Instruction history (${versions.length} versions)`),
      h('table', {}, h('tbody', {}, versions.map((v, i) => h('tr', {},
        h('td', {}, fmtStamp(v.created_at)), h('td', {}, v.note || ''),
        h('td', {}, i === 0 ? h('span', { class: 'hint' }, 'current') : h('button', { class: 'btn ghost', onclick: async () => {
          if (!confirm('Restore the rules, source contexts and category descriptions from this version?')) return;
          await api(`/api/instructions/versions/${v.id}/restore`, { method: 'POST' }); toast('Restored'); render();
        } }, 'Restore'))))))),
    h('div', { class: 'toolbar', style: { marginTop: '8px' } },
      h('button', { class: 'btn ghost', onclick: async (e) => {
        if (!confirm('Re-run the extractor on all your labeled examples with the current instructions? This costs one model call per labeled batch.')) return;
        e.target.disabled = true; e.target.textContent = 'Evaluating…';
        const rep = await api('/api/replay', { method: 'POST' });
        e.target.replaceWith(h('span', { class: 'hint' }, rep.summary));
      } }, 'Evaluate current instructions on my labels')),
  ];
}

// ---------------------------------------------------------------- footer credit
(async function renderCredit() {
  try {
    const i = await api('/api/info');
    const el = $('#credit');
    const link = (href, text) => href ? h('a', { href, target: '_blank', rel: 'noopener' }, text) : text;
    const parts = [`Chatter Radar v${i.version}`, ['by ', link(i.author_url, i.author)]];
    if (i.repo_url) parts.push(link(i.repo_url, 'GitHub'), link(i.issues_url, 'Report a problem'));
    el.replaceChildren(...parts.flatMap((p, n) => (n ? [' · ', ...[p].flat()] : [p])).map((x) => (x instanceof Node ? x : document.createTextNode(x))));
    el.hidden = false;
  } catch { /* the footer is optional */ }
})();
