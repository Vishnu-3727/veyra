// Veyra frontend — vanilla JS, no build step. Talks to the FastAPI
// backend over fetch(). Every render function is pure: takes data,
// returns/mutates DOM, never hides a failure silently (network/API errors
// surface as a toast or a persistent banner, not a blank screen).
'use strict';

const API_BASE = (() => {
  const stored = localStorage.getItem('veyra_api_base');
  if (stored) return stored;
  return `${location.protocol}//${location.hostname}:8000`;
})();

const STATUS_COLORS = { AUTO_MATCH: '#70C174', AI_ASSISTED_MATCH: '#7BA7D9', EXCEPTION: '#E06C65' };
const OUTCOME_COLORS = {
  CORRECT_AUTO: '#70C174', INCORRECT_AUTO: '#E06C65', MISSED_OPPORTUNITY: '#D9A441',
  UNSAFE_AUTO: '#8A342E', CORRECTLY_ESCALATED: '#7BA7D9',
};
const VIEW_TITLES = {
  overview: 'Overview', reconciliation: 'Reconciliation', exceptions: 'Exceptions',
  evaluation: 'Evaluation', audit: 'Audit Trail',
};

// ---------------------------------------------------------------------------
// icon system -- small inline stroke icons, no external dependency. Used to
// pull every surface away from a plain-text admin-template feel: metric
// cards get an icon badge, nav items get a leading glyph, log/badge rows get
// a status icon instead of a bare dot.
// ---------------------------------------------------------------------------

const ICONS = {
  alertTriangle: '<path d="M12 3l10 18H2z"/><path d="M12 9v5"/><circle cx="12" cy="17" r="0.6" fill="currentColor" stroke="none"/>',
  shieldCheck: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
  clipboardList: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 3h6v3H9z"/><path d="M8 11h8M8 14h8M8 17h5"/>',
  activity: '<path d="M3 12h4l2-7 4 14 2-7h6"/>',
  database: '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5"/><path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  percentCircle: '<circle cx="12" cy="12" r="9"/><path d="M8.5 8.5l7 7"/><circle cx="9" cy="9" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="15" r="1" fill="currentColor" stroke="none"/>',
  xCircle: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  bolt: '<path d="M13 2 4 14h6l-1 8 9-12h-6z"/>',
};
function icon(name, cls = '') {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
}

// Page sizes. /decisions pages with offset (server cap: limit<=1000); /exceptions
// and /audit have no offset, so "load more" raises their limit up to API_MAX_LIMIT.
const PAGE_STEP = 300;
const DECISIONS_PAGE = 300;
const API_MAX_LIMIT = 2000;

const state = {
  meta: null, health: null, runs: [], runId: null, runDetail: null,
  evaluation: null, baseline: null, charts: {}, excCategory: '',
  decisionsOffset: 0, decisionsShown: 0, excLimit: PAGE_STEP, auditLimit: PAGE_STEP,
  lastRunError: null,
};

// ---------------------------------------------------------------------------
// fetch helpers
// ---------------------------------------------------------------------------

let inflight = 0;
function setLoading(on) {
  inflight = Math.max(0, inflight + (on ? 1 : -1));
  const bar = document.getElementById('loadingBar');
  bar.style.width = inflight > 0 ? '70%' : '100%';
  if (inflight === 0) setTimeout(() => { bar.style.width = '0%'; }, 250);
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ` ${kind}` : '');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3200);
}

// Optional API auth: when the server has API_AUTH_TOKEN set, every endpoint except
// /health answers 401. The operator supplies the token at runtime and it is held in
// sessionStorage -- this tab, this session only. Never localStorage, never a URL
// parameter (URLs are logged by browsers and proxies), never a committed file.
const API_TOKEN_KEY = 'veyra_api_token';
function getApiToken() {
  try { return sessionStorage.getItem(API_TOKEN_KEY) || ''; } catch { return ''; }
}

// Set as soon as any endpoint answers 401, so the shell can say "auth required"
// rather than "backend unreachable" when the bootstrap chain fails.
let authRequired = false;

async function request(method, path, params = {}, jsonBody = null) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''));
  const url = `${API_BASE}${path}${qs.toString() ? '?' + qs.toString() : ''}`;
  setLoading(true);
  try {
    const opts = { method, headers: {} };
    const token = getApiToken();
    if (token) opts.headers['X-API-Token'] = token;
    if (jsonBody !== null) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(jsonBody);
    }
    const res = await fetch(url, opts);
    const body = await res.json().catch(() => ({}));
    // 401 is never a data problem: without the token the whole dashboard would render
    // empty, so surface a persistent prompt instead of a stack of identical toasts.
    if (res.status === 401) {
      authRequired = true;
      showAuthPrompt();
      throw new Error(body.detail || 'Unauthorized -- dashboard API token required.');
    }
    if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
    return body;
  } catch (e) {
    if (e instanceof TypeError) throw new Error(`Cannot reach API at ${API_BASE} -- is it running? (uvicorn app.api:app)`);
    throw e;
  } finally {
    setLoading(false);
  }
}
const apiGet = (path, params) => request('GET', path, params);
const apiPost = (path, params) => request('POST', path, params);
// Sensitive payloads (e.g. the LLM API key) go in a JSON body, never in the URL -- URLs are
// routinely logged by browsers, proxies, and monitoring tools.
const apiPostJson = (path, jsonBody) => request('POST', path, {}, jsonBody);

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

const fmtPct = (x, d = 1) => (x === null || x === undefined) ? '—' : `${(x * 100).toFixed(d)}%`;
const fmtAmount = (x) => (x === null || x === undefined || x === '') ? 'N/A' : `₹${Number(x).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const fmtNum = (x) => (x === null || x === undefined) ? '—' : Number(x).toLocaleString('en-IN');
const fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch { return '—'; } };
const catLabel = (c) => (state.meta && c && state.meta.category_labels[c]) || c || '—';
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function statusBadge(status) {
  const cls = status === 'AUTO_MATCH' ? 'auto' : status === 'AI_ASSISTED_MATCH' ? 'ai' : 'exception';
  const label = (state.meta && state.meta.statuses[status]) || status;
  const ic = status === 'EXCEPTION' ? 'alertTriangle' : status === 'AI_ASSISTED_MATCH' ? 'cpu' : 'checkCircle';
  return `<span class="badge ${cls}">${icon(ic, 'badge-icon')}${escapeHtml(label)}</span>`;
}

// generic pill-filter row builder, backed by app state rather than a <select>
function buildChips(container, entries, activeValue, onChange) {
  container.innerHTML = entries.map(([val, label, count]) => `
    <button type="button" class="chip ${val === activeValue ? 'active' : ''}" data-val="${escapeHtml(val)}">
      ${escapeHtml(label)}${count !== undefined ? `<span class="n">${count}</span>` : ''}
    </button>`).join('');
  container.querySelectorAll('.chip').forEach((btn) => {
    btn.addEventListener('click', () => onChange(btn.dataset.val));
  });
}

// ---------------------------------------------------------------------------
// evidence checklist -- the core "why was this safe / unsafe" UI
// ---------------------------------------------------------------------------

function amountState(pct) {
  const t = state.meta.thresholds;
  if (pct === undefined || pct === null) return 'bad';
  if (pct <= t.exact_amount_tolerance_pct) return 'ok';
  if (pct <= t.ai_hard_amount_mismatch_cap_pct) return 'warn';
  return 'bad';
}
function dateState(days) {
  const t = state.meta.thresholds;
  if (days === undefined || days === null || days < 0) return 'bad';
  if (days <= 2) return 'ok';
  if (days <= t.settlement_window_days) return 'warn';
  return 'bad';
}
function nameState(sim) {
  const t = state.meta.thresholds;
  if (sim === undefined || sim === null) return 'bad';
  if (sim >= t.high_name_similarity) return 'ok';
  if (sim >= t.min_name_similarity_for_ai) return 'warn';
  return 'bad';
}
function refState(ref) {
  if (ref === 'EXACT') return 'ok';
  if (ref === 'PARTIAL') return 'warn';
  return 'bad';
}
const CHECK_ICON = { ok: '\u2713', warn: '!', bad: '\u2715' };
const EVIDENCE_QUALIFIER = { ok: 'strong evidence', warn: 'supporting evidence', bad: 'weak evidence' };

function extractCandidate(decision) {
  const ev = decision.evidence || {};
  if (decision.status === 'EXCEPTION') {
    const list = (ev.evidence_found && ev.evidence_found.candidates_considered) || [];
    return list[0] || null;
  }
  return ev.matched_candidate || null;
}

function renderChecklist(c) {
  if (!c) return '<div class="empty-state"><div class="es-body">No candidate bank record was found to evaluate.</div></div>';
  const rows = [
    { label: 'Amount', value: fmtAmount(c.amount), detail: `\u0394 ${fmtPct(c.amount_diff_pct, 2)}`, state: amountState(c.amount_diff_pct) },
    { label: 'Settlement date', value: c.settlement_date || '—', detail: `${c.date_diff_days >= 0 ? '+' : ''}${c.date_diff_days}d vs. payment`, state: dateState(c.date_diff_days) },
    { label: 'Customer', value: c.payer_name || '—', detail: `similarity ${c.name_similarity}/100`, state: nameState(c.name_similarity) },
    { label: 'Reference', value: c.ref_match === 'NONE' ? 'Not found' : (c.reference_hint || c.utr || '—'), detail: c.ref_match, state: refState(c.ref_match) },
  ];
  return `<div class="checklist">${rows.map((r) => `
    <div class="check-row">
      <span class="check-icon ${r.state}">${CHECK_ICON[r.state]}</span>
      <span class="check-label">${r.label}</span>
      <span class="check-value">${escapeHtml(String(r.value))}</span>
      <span class="check-detail">${escapeHtml(r.detail)}</span>
      <span class="check-qualifier">+ ${EVIDENCE_QUALIFIER[r.state]}</span>
    </div>`).join('')}</div>`;
}

function renderVerdict(decision) {
  const matched = decision.status !== 'EXCEPTION';
  const cls = matched ? 'approved' : 'blocked';
  const title = !matched ? 'Automation blocked'
    : decision.status === 'AI_ASSISTED_MATCH' ? 'AI-assisted match approved' : 'Match approved';
  return `<div class="verdict-box ${cls}"><div class="verdict-title">${title}</div><div class="verdict-text">${escapeHtml(decision.reason || '')}</div></div>`;
}

// recursive fallback renderer for raw evidence JSON (candidate lists, AI reasoning, etc.)
function renderKV(value, depth = 0) {
  if (value === null || value === undefined) return '<span class="v-null">null</span>';
  if (Array.isArray(value)) {
    if (value.length === 0) return '<span class="v-null">[]</span>';
    if (typeof value[0] !== 'object') return value.map((v) => renderKV(v)).join(', ');
    return value.map((item, i) => `<div class="arr-item"><div class="faint" style="font-size:10.5px;margin-bottom:4px">#${i}</div>${renderKV(item, depth + 1)}</div>`).join('');
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value);
    if (entries.length === 0) return '<span class="v-null">{}</span>';
    return `<div class="${depth > 0 ? 'indent' : ''}">${entries.map(([k, v]) => {
      const isLeaf = v === null || typeof v !== 'object' || (Array.isArray(v) && v.length === 0);
      return `<div><span class="k">${escapeHtml(k)}</span>: ${isLeaf ? renderKV(v) : ''}</div>${!isLeaf ? renderKV(v, depth + 1) : ''}`;
    }).join('')}</div>`;
  }
  if (typeof value === 'number') return `<span class="v-num">${value}</span>`;
  return `<span class="v-str">"${escapeHtml(value)}"</span>`;
}

// ---------------------------------------------------------------------------
// charts (Chart.js, themed to match the design system)
//
// Charts here are decorative: every figure they plot is also rendered as text
// (KPI cards, tables, proportion bars). Chart.js is vendored locally, but if the
// file is missing, blocked, or fails to evaluate, `Chart` is simply undefined --
// touching it at module scope would abort the whole script and blank the app, and
// throwing inside a render function would truncate that view. Both paths are
// therefore guarded and degrade to an inline notice.
// ---------------------------------------------------------------------------

if (typeof Chart !== 'undefined') {
  Chart.defaults.color = '#A8A49B';
  Chart.defaults.font.family = "'Inter', sans-serif";
  Chart.defaults.borderColor = '#26251F';
  // Every chart lives in a `.chart-box` with an explicit CSS height. Without
  // this, Chart.js sizes the canvas from its own aspect ratio based on
  // container width and ignores that height, overflowing past the panel.
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.responsive = true;
}

let chartLibWarned = false;
function chartUnavailable(canvas, err) {
  if (!chartLibWarned) {
    chartLibWarned = true;
    console.warn('Veyra: Chart.js unavailable — charts replaced with a notice, all figures remain rendered as text.', err || '');
  }
  canvas.style.display = 'none';
  const box = canvas.parentElement;
  if (box && !box.querySelector('.chart-fallback')) {
    const note = document.createElement('div');
    note.className = 'chart-fallback';
    note.textContent = 'Chart library unavailable — figures above are unaffected.';
    box.appendChild(note);
  }
}

function upsertChart(key, canvasId, config) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (state.charts[key]) {
    try { state.charts[key].destroy(); } catch (e) { /* a half-constructed chart is not worth a stack trace */ }
    delete state.charts[key];
  }
  if (typeof Chart === 'undefined') { chartUnavailable(ctx); return; }
  try {
    state.charts[key] = new Chart(ctx, config);
  } catch (e) {
    chartUnavailable(ctx, e);
    return;
  }
  // A previous failure may have left the canvas hidden behind a notice.
  ctx.style.display = '';
  const stale = ctx.parentElement && ctx.parentElement.querySelector('.chart-fallback');
  if (stale) stale.remove();
}

// ---------------------------------------------------------------------------
// run status helpers
//
// A run row is RUNNING | COMPLETED | FAILED, and only a COMPLETED run is
// guaranteed to carry full metrics (total_payments, status_counts, timings). A
// RUNNING or FAILED run may carry a partial dict or none at all, so every read
// goes through these helpers and every KPI surface checks hasRunMetrics() first
// -- a partial run must explain itself, not throw on `undefined.status_counts`.
// ---------------------------------------------------------------------------

function runMetrics() {
  const m = state.runDetail && state.runDetail.metrics;
  return (m && typeof m === 'object') ? m : {};
}
function statusCounts() {
  const sc = runMetrics().status_counts;
  return (sc && typeof sc === 'object') ? sc : {};
}
function runStatus() {
  return (state.runDetail && state.runDetail.status) || 'COMPLETED';
}
function hasRunMetrics() {
  const m = runMetrics();
  return !!state.runDetail && m.total_payments !== null && m.total_payments !== undefined && !!m.status_counts;
}
function fmtSeconds(x, d = 2) {
  return (typeof x === 'number' && isFinite(x)) ? `${x.toFixed(d)}s` : '—';
}
// Status suffix for run labels; a COMPLETED run needs no annotation.
function runStatusSuffix(status) {
  return (status && status !== 'COMPLETED') ? ` — ${status}` : '';
}

// ---------------------------------------------------------------------------
// landing -> app, scroll-reveal, "see how it works"
// ---------------------------------------------------------------------------

const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => { if (entry.isIntersecting) entry.target.classList.add('in'); });
}, { threshold: 0.15 });
document.querySelectorAll('.reveal').forEach((el) => revealObserver.observe(el));

function enterApp() {
  document.getElementById('landing').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  window.scrollTo({ top: 0 });
}
for (const id of ['btnEnter', 'btnEnterHero', 'btnEnterFinal']) {
  document.getElementById(id).addEventListener('click', enterApp);
}
function updateLandingStats() {
  const complete = hasRunMetrics();
  const m = runMetrics();
  const total = complete ? m.total_payments : null;
  document.getElementById('landingStatusLine').innerHTML = total
    ? `<span class="stat-chip">${icon('database')}<b>${fmtNum(total)}</b> records</span><span class="stat-chip">${icon('activity')}latest run <b>${escapeHtml(state.runId)}</b></span><span class="stat-chip">${icon('clock')}<b>${fmtSeconds(m.total_processing_seconds)}</b> to process</span>`
    : '';

  // hero mockup preview -- a live miniature of the real Overview, not a
  // decorative fake chart. Reuses the same run metrics rendered elsewhere.
  const kpis = document.querySelectorAll('#heroPreviewBody .hpv-kpi .v');
  if (complete && total > 0 && kpis.length === 2) {
    const sc = statusCounts();
    const reconciled = (sc.AUTO_MATCH || 0) + (sc.AI_ASSISTED_MATCH || 0);
    kpis[0].textContent = fmtNum(total);
    kpis[1].textContent = fmtNum(reconciled);
    const segs = [
      { n: sc.AUTO_MATCH || 0, c: 'var(--success)' },
      { n: sc.AI_ASSISTED_MATCH || 0, c: 'var(--info)' },
      { n: sc.EXCEPTION || 0, c: 'var(--error)' },
    ].filter((s) => s.n > 0);
    document.getElementById('hpvBar').innerHTML = segs.map((s) => `<span style="width:${(s.n / total) * 100}%;background:${s.c}"></span>`).join('');
  }
  const rows = document.querySelectorAll('#heroPreviewBody .hpv-row');
  if (state.recentPreview && state.recentPreview.length && rows.length) {
    state.recentPreview.slice(0, rows.length).forEach((d, i) => {
      const ok = d.status !== 'EXCEPTION';
      rows[i].innerHTML = `<span class="d" style="background:${ok ? 'var(--success)' : 'var(--error)'}"></span>${escapeHtml(d.payment_id)} ${ok ? 'auto-matched' : 'exception'}`;
    });
  }
}

// ---------------------------------------------------------------------------
// sidebar: collapse (desktop) + off-canvas (mobile)
// ---------------------------------------------------------------------------

const SIDEBAR_KEY = 'veyra_sidebar_collapsed';
function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
}
document.getElementById('sidebarToggle').addEventListener('click', () => {
  setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
});
setSidebarCollapsed(localStorage.getItem(SIDEBAR_KEY) === '1');

document.getElementById('mobileNavToggle').addEventListener('click', () => {
  document.body.classList.add('sidebar-open');
});
document.getElementById('sidebarScrim').addEventListener('click', () => {
  document.body.classList.remove('sidebar-open');
});

// ---------------------------------------------------------------------------
// sidebar nav
// ---------------------------------------------------------------------------

function switchView(name) {
  document.querySelectorAll('.nav-item[data-view]').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  document.getElementById('topbarTitle').textContent = VIEW_TITLES[name] || name;
  document.body.classList.remove('sidebar-open');
  document.querySelector('.content').scrollTo?.({ top: 0 });
  window.scrollTo({ top: 0 });
}
document.querySelectorAll('.nav-item[data-view]').forEach((item) => {
  item.addEventListener('click', () => switchView(item.dataset.view));
});
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-goto]');
  if (link) { e.preventDefault(); switchView(link.dataset.goto); }
});

// ---------------------------------------------------------------------------
// system status (topbar) + API-offline banner
// ---------------------------------------------------------------------------

function renderAIStatus(health) {
  const pill = document.getElementById('aiStatusPill');
  const text = document.getElementById('aiStatusText');
  const dotAI = document.getElementById('dotAI');
  const statusAI = document.getElementById('statusAI');
  if (!health) {
    pill.className = 'status-pill off';
    text.textContent = 'API unreachable';
    dotAI.className = 'system-dot off';
    statusAI.textContent = 'OFFLINE';
    statusAI.className = 'system-row-status off';
  } else if (health.ai_enabled) {
    pill.className = 'status-pill on';
    text.textContent = `AI enabled (${health.llm_model})`;
    dotAI.className = 'system-dot on';
    statusAI.textContent = 'AVAILABLE';
    statusAI.className = 'system-row-status on';
  } else {
    pill.className = 'status-pill off';
    text.textContent = 'AI disabled — fallback mode';
    dotAI.className = 'system-dot off';
    statusAI.textContent = 'FALLBACK';
    statusAI.className = 'system-row-status off';
  }
}

// authBlocked = the API answered and refused us (401). That is NOT the same as an
// unreachable backend, and saying "Backend unreachable" there sends the operator to
// restart a server that is running fine.
function renderSystemStatus(ok, authBlocked = false) {
  const block = document.getElementById('systemStatusBlock');
  const banner = document.getElementById('apiOfflineBanner');
  block.classList.toggle('offline', !ok);
  document.getElementById('systemStatusText').textContent =
    ok ? 'SYSTEM OPERATIONAL' : authBlocked ? 'API AUTH REQUIRED' : 'API UNREACHABLE';
  // The auth banner already explains a 401, so do not stack the offline banner on top.
  banner.classList.toggle('show', !ok && !authBlocked);
  const down = authBlocked ? 'LOCKED' : 'OFFLINE';
  document.getElementById('statusRule').textContent = ok ? 'ACTIVE' : down;
  document.getElementById('statusRule').className = 'system-row-status ' + (ok ? 'on' : 'off');
  document.getElementById('statusPipeline').textContent = ok ? 'READY' : down;
  document.getElementById('statusPipeline').className = 'system-row-status ' + (ok ? 'on' : 'off');
}

function updateLastRunLabel() {
  const el = document.getElementById('systemLastRun');
  if (state.runDetail) {
    const status = runStatus();
    el.textContent = `Last run: ${fmtTime(state.runDetail.finished_at || state.runDetail.started_at)}${runStatusSuffix(status)}`;
  } else {
    el.textContent = 'No runs yet';
  }
}

// ---------------------------------------------------------------------------
// optional API auth prompt
//
// With API_AUTH_TOKEN set server-side, /health still answers but every data
// endpoint returns 401 -- which would look like an empty dashboard. The banner
// says what is actually wrong and takes the token for this browser session.
// ---------------------------------------------------------------------------

let authPromptDismissed = false;
function showAuthPrompt() {
  if (authPromptDismissed) return;
  document.getElementById('apiAuthBanner').classList.add('show');
}
function hideAuthPrompt() {
  document.getElementById('apiAuthBanner').classList.remove('show');
}
document.getElementById('btnSaveApiToken').addEventListener('click', () => {
  const input = document.getElementById('apiTokenInput');
  const token = input.value.trim();
  if (!token) { toast('Enter the dashboard API token first.', 'error'); return; }
  try {
    sessionStorage.setItem(API_TOKEN_KEY, token);
  } catch (e) {
    toast('This browser blocks sessionStorage — the token cannot be stored.', 'error');
    return;
  }
  input.value = '';
  authPromptDismissed = false;
  hideAuthPrompt();
  bootstrap();
});
document.getElementById('apiTokenInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('btnSaveApiToken').click();
});
document.getElementById('btnDismissApiToken').addEventListener('click', () => {
  authPromptDismissed = true;
  hideAuthPrompt();
});

document.getElementById('btnRetryApi').addEventListener('click', () => bootstrap());

// ---------------------------------------------------------------------------
// bootstrap
// ---------------------------------------------------------------------------

async function loadMetaAndHealth() {
  const [meta, health] = await Promise.all([apiGet('/meta'), apiGet('/health').catch(() => null)]);
  state.meta = meta;
  state.health = health;
  renderAIStatus(health);
  renderSystemStatus(!!health);
  document.getElementById('apiTarget').textContent = API_BASE;

  const statusSel = document.getElementById('filterStatus');
  statusSel.querySelectorAll('option:not(:first-child)').forEach((o) => o.remove());
  Object.entries(meta.statuses).forEach(([k, v]) => statusSel.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`));
  const catSel = document.getElementById('filterCategory');
  catSel.querySelectorAll('option:not(:first-child)').forEach((o) => o.remove());
  Object.entries(meta.category_labels).forEach(([k, v]) => catSel.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`));
}

async function loadRuns(selectRunId) {
  try {
    state.runs = await apiGet('/runs', { limit: 25 });
  } catch (e) {
    state.runs = [];
  }
  const sel = document.getElementById('runSelect');
  sel.innerHTML = '';
  if (state.runs.length === 0) {
    sel.insertAdjacentHTML('beforeend', `<option value="">No runs yet</option>`);
    state.runId = null;
    return;
  }
  state.runs.forEach((r) => {
    // A RUNNING or FAILED run must never look like a finished one in the picker,
    // and its record count may not exist yet.
    const records = (r.total_payments === null || r.total_payments === undefined)
      ? 'no records yet' : `${r.total_payments} records`;
    const label = `${r.run_id} (${records})${runStatusSuffix(r.status)}`;
    sel.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(r.run_id)}">${escapeHtml(label)}</option>`);
  });
  sel.value = selectRunId && state.runs.some((r) => r.run_id === selectRunId) ? selectRunId : state.runs[0].run_id;
  state.runId = sel.value;
}

document.getElementById('runSelect').addEventListener('change', (e) => {
  state.runId = e.target.value;
  loadRunData();
});

// Overview has three mutually exclusive bodies: the KPI dashboard, the
// "no runs yet" call to action, and the partial-run explanation.
function setOverviewMode(mode) {
  document.getElementById('overviewData').style.display = mode === 'data' ? 'block' : 'none';
  document.getElementById('overviewEmpty').style.display = mode === 'no-runs' ? 'block' : 'none';
  document.getElementById('overviewIncomplete').style.display = mode === 'incomplete' ? 'block' : 'none';
}

async function loadRunData() {
  if (!state.runId) {
    document.getElementById('overviewCaption').textContent = 'Evidence-driven reconciliation for merchant finance operations.';
    document.getElementById('overviewStatusLine').innerHTML = '';
    setOverviewMode('no-runs');
    updateLastRunLabel();
    updateLandingStats();
    return;
  }
  try {
    const [detail, evaluation, baseline] = await Promise.all([
      apiGet(`/runs/${state.runId}`),
      apiGet(`/runs/${state.runId}/evaluation`).catch(() => null),
      // Run-scoped: the baseline must be computed on the SAME dataset snapshot as
      // the selected run, otherwise the comparison silently mixes two datasets.
      apiGet('/baseline', { run_id: state.runId }).catch(() => null),
    ]);
    state.runDetail = detail;
    state.evaluation = evaluation;
    state.baseline = baseline;
    renderOverview();
    renderEvaluation();
    updateLandingStats();
    updateLastRunLabel();
    await Promise.all([refreshDecisions(), refreshExceptions(), refreshAudit(), refreshRecent()]);
    updateLandingStats();
    loadStoryExamples();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

function heroKpi(label, value, cls = '', iconName = 'activity', tint = 'neutral', sub = '') {
  return `<div class="hero-kpi">
    <div class="hero-kpi-top"><span class="icon-badge ${tint}">${icon(iconName)}</span></div>
    <div class="hero-kpi-value ${cls}">${value}</div>
    <div class="hero-kpi-label">${label}</div>
    ${sub ? `<div class="hero-kpi-sub">${sub}</div>` : ''}
  </div>`;
}
function slimStat(label, value, iconName = 'activity') {
  return `<div class="metric-secondary"><span class="metric-secondary-value">${value}</span><span class="metric-secondary-label">${icon(iconName, 'slim-icon')}${label}</span></div>`;
}

// A RUNNING or FAILED run carries no usable metrics: say what happened (and why,
// when the backend recorded an error) instead of rendering zeros as if they were
// measurements.
function renderIncompleteRun() {
  const status = runStatus();
  const err = state.runDetail && state.runDetail.error;
  const title = status === 'RUNNING' ? 'Reconciliation in progress' : `Run ${status}`;
  const body = status === 'RUNNING'
    ? 'This run has not finished, so its metrics are still partial. Re-select the run once it completes.'
    : 'This run did not complete, so it has no trustworthy metrics. Nothing here is a measurement of a finished batch.';
  document.getElementById('overviewCaption').textContent = 'Evidence-driven reconciliation for merchant finance operations.';
  document.getElementById('overviewStatusLine').innerHTML =
    `<span class="stat-chip">${icon('activity')}run <b class="mono">${escapeHtml(state.runId)}</b></span><span class="stat-chip">${icon('alertTriangle')}status <b>${escapeHtml(status)}</b></span>`;
  document.getElementById('circuitBreakerWarning').style.display = 'none';
  document.getElementById('overviewIncomplete').innerHTML = `
    <div class="es-title">${escapeHtml(title)}</div>
    <div class="es-body">${escapeHtml(body)}</div>
    ${err ? `<div class="verdict-box blocked" style="text-align:left;max-width:640px;margin:var(--sp-4) auto 0">
      <div class="verdict-title">Reported error</div>
      <div class="verdict-text">${escapeHtml(String(err))}</div>
    </div>` : ''}`;
  setOverviewMode('incomplete');
}

function renderOverview() {
  if (!hasRunMetrics()) { renderIncompleteRun(); return; }
  setOverviewMode('data');
  const m = runMetrics();
  const sc = statusCounts();
  const total = m.total_payments;
  const auto = sc.AUTO_MATCH || 0, ai = sc.AI_ASSISTED_MATCH || 0, exc = sc.EXCEPTION || 0;
  const reconciled = auto + ai;
  const reconciliationRate = total ? reconciled / total : null;

  document.getElementById('overviewCaption').textContent = 'Evidence-driven reconciliation for merchant finance operations.';
  document.getElementById('overviewStatusLine').innerHTML =
    `<span class="stat-chip">${icon('database')}<b>${fmtNum(total)}</b> records</span><span class="stat-chip">${icon('activity')}run <b class="mono">${escapeHtml(state.runId)}</b></span><span class="stat-chip">${icon('clock')}<b>${fmtSeconds(m.total_processing_seconds)}</b> to process</span>`;

  const cbWarning = document.getElementById('circuitBreakerWarning');
  if (m.ai_circuit_breaker_tripped) {
    cbWarning.style.display = 'block';
    cbWarning.innerHTML = `<div class="verdict-box blocked" style="margin-bottom:var(--sp-6)">
      <div class="verdict-title">AI provider stopped responding mid-batch</div>
      <div class="verdict-text">${escapeHtml(m.ai_circuit_breaker_reason || '')} — remaining AI-eligible cases were routed straight to review instead of retrying a broken connection. <a href="#" onclick="document.getElementById('aiStatusPill').click(); return false;" style="color:var(--accent-bright)">Check provider settings →</a></div>
    </div>`;
  } else {
    cbWarning.style.display = 'none';
  }

  document.getElementById('heroKpis').innerHTML = [
    heroKpi('Records processed', fmtNum(total), '', 'database', 'neutral'),
    heroKpi('Automatically reconciled', fmtNum(reconciled), 'success', 'checkCircle', 'success'),
    heroKpi('Exceptions', fmtNum(exc), exc > 0 ? 'error' : '', 'alertTriangle', exc > 0 ? 'error' : 'neutral'),
    heroKpi('Reconciliation rate', fmtPct(reconciliationRate), 'accent', 'percentCircle', 'accent'),
  ].join('');

  document.getElementById('slimStats').innerHTML = [
    slimStat('throughput', m.throughput_per_second ? `${m.throughput_per_second.toFixed(1)} rec/s` : '—', 'bolt'),
    slimStat('processing time', fmtSeconds(m.total_processing_seconds), 'clock'),
    slimStat('avg / record', m.avg_processing_ms_per_record ? `${m.avg_processing_ms_per_record.toFixed(2)}ms` : '—', 'clock'),
    slimStat('AI invocations', fmtNum(m.ai_invocations), 'cpu'),
  ].join('');

  const proof = document.getElementById('proofStrip');
  // "Same dataset" is the entire claim of this strip, so it is shown only when the
  // baseline really was scored on this run's snapshot (see baselineIsRunScoped).
  if (state.evaluation && baselineIsRunScoped()) {
    proof.style.display = 'flex';
    document.getElementById('proofUs').textContent = fmtPct(state.evaluation.false_match_rate, 1);
    document.getElementById('proofBaseline').textContent = fmtPct(state.baseline.false_match_rate, 1);
  } else {
    proof.style.display = 'none';
  }

  // proportion bar
  const segs = [
    { key: 'auto', label: 'Auto-matched', n: auto },
    { key: 'ai', label: 'AI-assisted', n: ai },
    { key: 'exception', label: 'Exceptions', n: exc },
  ].filter((s) => s.n > 0);
  document.getElementById('propBar').innerHTML = total ? segs.map((s) => {
    const pct = (s.n / total) * 100;
    return `<div class="prop-seg ${s.key}" style="width:${pct}%">${pct >= 7 ? fmtPct(s.n / total, 0) : ''}</div>`;
  }).join('') : '';
  document.getElementById('propLegend').innerHTML = segs.map((s) =>
    `<span><span class="sw" style="background:${STATUS_COLORS[s.key === 'auto' ? 'AUTO_MATCH' : s.key === 'ai' ? 'AI_ASSISTED_MATCH' : 'EXCEPTION']}"></span>${s.label} (${fmtNum(s.n)})</span>`
  ).join('');

  // reason list (ranked bars, no chart)
  const cc = Object.entries(m.category_counts || {}).sort((a, b) => b[1] - a[1]);
  const maxCount = cc.length ? cc[0][1] : 1;
  const reasonList = document.getElementById('reasonList');
  if (cc.length === 0) {
    reasonList.innerHTML = '<div class="empty-state"><div class="es-title">No exceptions</div><div class="es-body">All records in this run were reconciled safely.</div></div>';
  } else {
    reasonList.innerHTML = cc.map(([k, v]) => `
      <div class="reason-row">
        <span class="rl-label">${escapeHtml(catLabel(k))}</span>
        <span class="rl-track"><span class="rl-fill" style="width:${(v / maxCount) * 100}%"></span></span>
        <span class="rl-count">${v}</span>
      </div>`).join('');
  }
}

async function refreshRecent() {
  if (!state.runId) return;
  const data = await apiGet('/decisions', { run_id: state.runId, limit: 6 });
  const matched = data.results.find((d) => d.status !== 'EXCEPTION');
  const exception = data.results.find((d) => d.status === 'EXCEPTION');
  state.recentPreview = [matched, exception].filter(Boolean);
  const tbody = document.querySelector('#recentTable tbody');
  document.querySelector('#recentTable thead').innerHTML = '<tr><th>Payment</th><th>Amount</th><th>Decision</th></tr>';
  tbody.innerHTML = data.results.map((d) => `
    <tr data-payment="${escapeHtml(d.payment_id)}" tabindex="0">
      <td class="mono">${escapeHtml(d.payment_id)}</td>
      <td class="mono">${fmtAmount(d.amount)}</td>
      <td>${statusBadge(d.status)}</td>
    </tr>`).join('');
  tbody.querySelectorAll('tr').forEach((row) => row.addEventListener('click', () => openDecisionDrawer(row.dataset.payment)));
}

// ---------------------------------------------------------------------------
// pagination footers
//
// Every list endpoint is capped server-side, so a rendered list is routinely a
// prefix of the truth. A truncated list must never imply completeness: show
// "N of TOTAL shown" and either offer the next page or say the cap was reached.
// ---------------------------------------------------------------------------

function renderPager(containerId, shown, total, atCap, onMore) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const known = typeof total === 'number';
  const parts = [`<span class="pager-count">${fmtNum(shown)} of ${known ? fmtNum(total) : '?'} shown</span>`];
  if (known && shown < total) {
    parts.push(atCap
      ? `<span class="pager-note">API page cap of ${fmtNum(API_MAX_LIMIT)} reached — ${fmtNum(total - shown)} more exist; filter to inspect them.</span>`
      : '<button class="btn" data-more="1">Load more</button>');
  }
  el.innerHTML = parts.join('');
  const btn = el.querySelector('[data-more]');
  if (btn) btn.addEventListener('click', () => { btn.disabled = true; onMore(); });
}

// ---------------------------------------------------------------------------
// Reconciliation (decisions table)
// ---------------------------------------------------------------------------

// append=false starts at offset 0 and replaces the table; append=true fetches the
// next offset page and adds to it.
async function refreshDecisions(append = false) {
  if (!state.runId) return;
  const status = document.getElementById('filterStatus').value;
  const category = document.getElementById('filterCategory').value;
  if (!append) { state.decisionsOffset = 0; state.decisionsShown = 0; }
  const data = await apiGet('/decisions', {
    run_id: state.runId, status, category, limit: DECISIONS_PAGE, offset: state.decisionsOffset,
  });
  state.decisionsShown += data.results.length;
  state.decisionsOffset = state.decisionsShown;
  document.getElementById('decisionsCount').textContent = `${fmtNum(state.decisionsShown)} of ${fmtNum(data.total)} shown`;

  const recon = document.getElementById('reconCaption');
  if (hasRunMetrics()) {
    const m = runMetrics();
    const sc = statusCounts();
    recon.innerHTML =
      `${fmtNum(m.total_payments)} records &nbsp;·&nbsp; <span style="color:var(--success)">${fmtNum((sc.AUTO_MATCH||0)+(sc.AI_ASSISTED_MATCH||0))} automatically reconciled</span> &nbsp;·&nbsp; <span style="color:var(--error)">${fmtNum(sc.EXCEPTION||0)} sent for review</span>`;
  } else {
    recon.innerHTML = `Run <b class="mono">${escapeHtml(state.runId)}</b> is ${escapeHtml(runStatus())} — batch totals are unavailable, so the rows below are only what this run recorded before it stopped.`;
  }

  const cols = ['payment_id', 'customer_name', 'amount', 'status', 'category', 'matched_bank_ref', 'confidence', 'method', 'ai_used'];
  document.querySelector('#decisionsTable thead').innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
  const tbody = document.querySelector('#decisionsTable tbody');
  if (!append && data.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state"><div class="es-title">No matching records</div><div class="es-body">No decisions match this filter combination.</div></div></td></tr>`;
    renderPager('decisionsPager', 0, data.total, false, () => refreshDecisions(true));
    return;
  }
  const rowsHtml = data.results.map((d) => `
    <tr data-payment="${escapeHtml(d.payment_id)}" tabindex="0">
      <td class="mono">${escapeHtml(d.payment_id)}</td>
      <td>${escapeHtml(d.customer_name || '')}</td>
      <td class="mono">${fmtAmount(d.amount)}</td>
      <td>${statusBadge(d.status)}</td>
      <td class="muted">${escapeHtml(catLabel(d.category))}</td>
      <td class="mono muted">${escapeHtml(d.matched_bank_ref || '—')}</td>
      <td class="mono">${d.confidence ?? '—'}</td>
      <td class="muted">${escapeHtml(d.method || '')}</td>
      <td>${d.ai_used ? 'yes' : 'no'}</td>
    </tr>`).join('');
  if (append) tbody.insertAdjacentHTML('beforeend', rowsHtml);
  else tbody.innerHTML = rowsHtml;
  // onclick (not addEventListener) so re-binding after an append never doubles up
  tbody.querySelectorAll('tr[data-payment]').forEach((row) => {
    row.onclick = () => openDecisionDrawer(row.dataset.payment);
  });
  renderPager('decisionsPager', state.decisionsShown, data.total, false, () => refreshDecisions(true));
}
document.getElementById('filterStatus').addEventListener('change', () => refreshDecisions());
document.getElementById('filterCategory').addEventListener('change', () => refreshDecisions());

async function openDecisionDrawer(paymentId) {
  try {
    // encodeURIComponent: payment_id comes from the source CSV, so a value containing / ? or #
    // would otherwise rewrite the request path instead of being sent as one path segment.
    const detail = await apiGet(`/decisions/${encodeURIComponent(paymentId)}`, { run_id: state.runId });
    const { payment, decision, audit_trail, exception } = detail;
    const candidate = extractCandidate(decision);
    document.getElementById('drawerTitle').textContent = paymentId;
    const candidateBit = decision.invoice_id ? ` — candidate ${escapeHtml(decision.invoice_id)}` : '';
    document.getElementById('drawerSubtitle').innerHTML = `${escapeHtml(payment?.customer_name || '')} — ${fmtAmount(payment?.amount)}${candidateBit}`;

    let resolveButtonHtml = '';
    if (exception) {
      resolveButtonHtml = exception.resolved
        ? `<span class="badge resolved" style="margin-top:14px">Marked resolved</span>`
        : `<button class="btn btn-danger-outline" id="btnResolve" style="margin-top:14px" data-exc="${Number(exception.id)}">Mark reviewed</button>`;
    }

    const confidencePct = decision.confidence !== null && decision.confidence !== undefined
      ? `<div class="confidence-block"><span class="cv" style="color:${decision.status === 'EXCEPTION' ? 'var(--error)' : 'var(--success)'}">${decision.confidence}%</span><span class="cl">confidence</span></div>` : '';

    document.getElementById('drawerBody').innerHTML = `
      <div>${statusBadge(decision.status)} ${decision.category ? `<span class="badge exception" style="margin-left:6px">${escapeHtml(catLabel(decision.category))}</span>` : ''}</div>
      <h4>Evidence checklist</h4>
      ${renderChecklist(candidate)}
      ${confidencePct}
      ${renderVerdict(decision)}
      ${resolveButtonHtml}
      <h4>Decision facts</h4>
      <div class="kv-tree">${renderKV({
        confidence: decision.confidence, method: decision.method, ai_used: !!decision.ai_used,
        matched_bank_ref: decision.matched_bank_ref, invoice_id: decision.invoice_id, invoice_status: decision.invoice_status,
        processing_ms: decision.processing_ms, created_at: decision.created_at,
      })}</div>
      <details>
        <summary>Full raw evidence &amp; audit trail</summary>
        <div class="kv-tree" style="margin-top:10px">${renderKV(decision.evidence || {})}</div>
        <div class="kv-tree" style="margin-top:10px">${audit_trail.map((a) => renderKV({ actor: a.actor, status: a.status, category: a.category, confidence: a.confidence, created_at: a.created_at })).join('<hr style="border-color:var(--border-soft);margin:8px 0">')}</div>
      </details>
    `;
    const btn = document.getElementById('btnResolve');
    if (btn) btn.addEventListener('click', () => resolveException(btn.dataset.exc, btn));
    openDrawer();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function resolveException(exceptionId, btnEl) {
  try {
    // The endpoint is idempotent: already_reviewed tells us whether this call was
    // the one that flipped the row, so the operator is not told a fresh review
    // happened when the exception was already signed off.
    const res = await apiPost(`/exceptions/${encodeURIComponent(exceptionId)}/resolve`);
    if (btnEl) btnEl.outerHTML = `<span class="badge resolved" style="margin-top:14px">Marked resolved</span>`;
    toast(res && res.already_reviewed ? 'Exception was already marked reviewed.' : 'Exception marked as reviewed.', 'success');
    refreshExceptions(true);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function openDrawer() {
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawerOverlay').classList.add('open');
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawerOverlay').classList.remove('open');
}
document.getElementById('drawerClose').addEventListener('click', closeDrawer);
document.getElementById('drawerOverlay').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDrawer(); closeSettingsModal(); } });

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

// keepLimit=true preserves the page size raised by "Load more"; a filter change or a
// run change resets it. /exceptions has no offset parameter, so paging means asking
// for a bigger limit, capped by the API at API_MAX_LIMIT.
async function refreshExceptions(keepLimit = false) {
  if (!state.runId) return;
  if (!keepLimit) state.excLimit = PAGE_STEP;
  const complete = hasRunMetrics();
  const sc = statusCounts();
  const cc = Object.entries(runMetrics().category_counts || {}).sort((a, b) => b[1] - a[1]);

  const data = await apiGet('/exceptions', { run_id: state.runId, category: state.excCategory, limit: state.excLimit });
  // data.total is scoped to the active category filter, so it only stands in for the
  // run-wide exception count when no filter is applied and metrics are missing.
  const runTotal = complete ? (sc.EXCEPTION || 0) : (state.excCategory ? null : data.total);

  document.getElementById('excCount').textContent = runTotal || '';
  document.getElementById('excHeaderCount').textContent = runTotal === null
    ? `Run ${runStatus()} — the complete exception count for this run is not available.`
    : runTotal === 0
      ? 'All records were reconciled safely.'
      : `${fmtNum(runTotal)} record${runTotal === 1 ? '' : 's'} require attention.`;

  // summary strip: total + top categories, all from real category_counts
  const summaryCards = [heroKpi('Total exceptions', runTotal === null ? '—' : fmtNum(runTotal), runTotal > 0 ? 'error' : '', 'alertTriangle', runTotal > 0 ? 'error' : 'neutral')]
    .concat(cc.slice(0, 3).map(([k, v]) => heroKpi(escapeHtml(catLabel(k)), fmtNum(v), '', 'clipboardList', 'neutral')));
  document.getElementById('excSummary').innerHTML = summaryCards.join('');

  // filter chips, built from real categories present in meta + counts from this run
  const chipEntries = [['', 'All', runTotal === null ? undefined : runTotal]].concat(cc.map(([k, v]) => [k, catLabel(k), v]));
  buildChips(document.getElementById('excFilterChips'), chipEntries, state.excCategory, (val) => {
    state.excCategory = val;
    refreshExceptions();
  });

  const loadMore = () => {
    state.excLimit = Math.min(state.excLimit + PAGE_STEP, API_MAX_LIMIT);
    refreshExceptions(true);
  };
  const list = document.getElementById('exceptionsList');
  if (data.results.length === 0) {
    list.innerHTML = `<div class="empty-state"><div class="es-title">No exceptions</div><div class="es-body">${runTotal === 0 ? 'All records in this run were reconciled safely.' : 'No exceptions match this filter.'}</div></div>`;
    renderPager('exceptionsPager', 0, data.total, state.excLimit >= API_MAX_LIMIT, loadMore);
    return;
  }
  renderPager('exceptionsPager', data.results.length, data.total, state.excLimit >= API_MAX_LIMIT, loadMore);
  list.innerHTML = data.results.map((r, i) => {
    const ev = r.evidence || {};
    const candidate = (ev.evidence_found && ev.evidence_found.candidates_considered && ev.evidence_found.candidates_considered[0]) || null;
    return `<div class="exc-card ${r.resolved ? 'is-resolved' : ''}" id="exc-${i}">
      <div class="exc-head" onclick="document.getElementById('exc-${i}').classList.toggle('open')">
        ${icon('alertTriangle', 'exc-icon')}
        <span class="chev">&#9656;</span>
        <span class="exc-title"><span class="pid">${escapeHtml(r.payment_id)}</span> — ${escapeHtml(r.customer_name || '')} — ${fmtAmount(r.amount)}</span>
        ${r.resolved ? '<span class="badge resolved">Resolved</span>' : `<span class="badge exception">${icon('alertTriangle', 'badge-icon')}${escapeHtml(catLabel(r.category))}</span>`}
      </div>
      <div class="exc-body"><div class="exc-body-inner">
        <h4 style="margin-top:0">Evidence checklist</h4>
        ${renderChecklist(candidate)}
        <dl>
          <dt>Why unresolved</dt><dd>${escapeHtml(ev.why_unresolved || r.reason || '')}</dd>
          <dt>Suggested next action</dt><dd>${escapeHtml(r.suggested_action || '')}</dd>
        </dl>
        ${r.resolved ? '' : `<button class="btn btn-danger-outline" data-exc="${Number(r.id)}" onclick="resolveException(${Number(r.id)}, this)">Mark reviewed</button>`}
      </div></div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

function cmpRow(label, us, them, higherIsBetter = true) {
  const usPct = Math.max(0, Math.min(100, us * 100));
  const themPct = Math.max(0, Math.min(100, them * 100));
  const delta = higherIsBetter ? (us - them) * 100 : (them - us) * 100;
  const sign = delta >= 0 ? '+' : '';
  return `<div class="cmp-row">
    <span class="cmp-label">${label}</span>
    <span class="cmp-bars">
      <span class="cmp-bar-track"><span class="cmp-bar-fill us" style="width:${usPct}%"></span></span>
      <span class="cmp-bar-track"><span class="cmp-bar-fill them" style="width:${themPct}%"></span></span>
    </span>
    <span class="cmp-delta">${sign}${delta.toFixed(1)}pp</span>
  </div>`;
}

// The baseline is comparable with a run's evaluation only when it was scored on
// that run's own dataset snapshot.
function baselineIsRunScoped() {
  const b = state.baseline;
  if (!b) return false;
  return b.source !== 'current_raw_file_fallback' && (!b.run_id || b.run_id === state.runId);
}

function renderEvaluation() {
  const ev = state.evaluation;
  if (!ev) {
    // Never leave the previous run's numbers on screen: a RUNNING or FAILED run has
    // no evaluation of its own, and showing a stale one attributes it to this run.
    document.getElementById('evalRecordCaption').innerHTML =
      `<span style="color:var(--warning)">No evaluation for run <b class="mono">${escapeHtml(String(state.runId || '—'))}</b> (status ${escapeHtml(runStatus())}) — a run is only scored once it completes.</span>`;
    document.getElementById('evalHeroKpis').innerHTML = '';
    document.getElementById('evalZeroBanner').style.display = 'none';
    document.getElementById('baselineCompare').innerHTML = '<div class="empty-state"><div class="es-body">No baseline comparison is available for this run.</div></div>';
    document.querySelector('#caseTypeTable thead').innerHTML = '';
    document.querySelector('#caseTypeTable tbody').innerHTML = '';
    return;
  }
  const o = ev.outcomes;

  // A run that predates per-run ground-truth snapshots cannot be re-scored against
  // the data it actually saw, so its evaluation is not reproducible. Say exactly that.
  const gtSourceNote = ev.ground_truth_source === 'current_raw_file_fallback'
    ? ' <span style="color:var(--warning)">(this run predates per-run ground-truth snapshots — scored against the current on-disk ground truth, which may have changed since the run, so this evaluation is NOT reproducible)</span>'
    : '';
  document.getElementById('evalRecordCaption').innerHTML =
    `${fmtNum(ev.source_records)} ground-truth records &nbsp;·&nbsp; ${fmtNum(ev.joined_records)} evaluated &nbsp;·&nbsp; ` +
    `${fmtNum(ev.dropped_records)} dropped at ingestion (not fabricated as correct or incorrect)${gtSourceNote}`;

  document.getElementById('evalHeroKpis').innerHTML = [
    heroKpi('Automation precision', fmtPct(ev.automation_precision), 'success', 'checkCircle', 'success'),
    heroKpi('Coverage / recall', fmtPct(ev.coverage_recall), '', 'activity', 'info'),
    heroKpi('Safety rate', fmtPct(ev.safety_rate), 'success', 'shieldCheck', 'success'),
    heroKpi('False-match rate', fmtPct(ev.false_match_rate, 2), ev.false_match_rate > 0 ? 'error' : 'success', 'xCircle', ev.false_match_rate > 0 ? 'error' : 'success'),
  ].join('');

  const zeroItems = [];
  if (ev.false_match_rate === 0) zeroItems.push('Zero false matches');
  if ((o.UNSAFE_AUTO || 0) === 0) zeroItems.push('Zero unsafe auto-resolutions');
  const banner = document.getElementById('evalZeroBanner');
  if (zeroItems.length) {
    banner.style.display = 'flex';
    banner.innerHTML = zeroItems.map((t) => `<span class="zb-item">${t}</span>`).join('');
  } else {
    banner.style.display = 'none';
  }

  upsertChart('outcomes', 'chartOutcomes', {
    type: 'bar',
    data: {
      labels: Object.keys(o),
      datasets: [{ data: Object.values(o), backgroundColor: Object.keys(o).map((k) => OUTCOME_COLORS[k]), borderRadius: 10, maxBarThickness: 72, categoryPercentage: 0.6, barPercentage: 0.9 }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#1B1A16' } }, x: { grid: { display: false } } } },
  });

  const baselineEl = document.getElementById('baselineCompare');
  if (state.baseline) {
    const b = state.baseline;
    // The comparison is only apples-to-apples when the baseline was scored on this
    // run's own snapshot. A fallback source, or a snapshot belonging to another run,
    // means two different datasets are being put side by side.
    const notRunScoped = !baselineIsRunScoped();
    const caveat = notRunScoped
      ? `<div class="muted" style="color:var(--warning);font-size:12.5px;margin-bottom:var(--sp-3)">Not run-scoped: this baseline was computed on ${b.source === 'current_raw_file_fallback' ? 'the current on-disk dataset' : `run ${escapeHtml(String(b.run_id))}`}, not on the dataset of run ${escapeHtml(String(state.runId))}. Treat the two columns as different datasets, not as a like-for-like comparison.</div>`
      : '';
    baselineEl.innerHTML = `
      ${caveat}
      <div class="cmp-legend"><span><span class="sw" style="background:var(--success)"></span>Veyra</span><span><span class="sw" style="background:var(--error)"></span>Naive baseline</span></div>
      ${cmpRow('Precision', ev.automation_precision, b.automation_precision)}
      ${cmpRow('Coverage', ev.coverage_recall, b.coverage_recall)}
      ${cmpRow('Safety rate', ev.safety_rate, b.safety_rate)}
      ${cmpRow('False-match rate', ev.false_match_rate, b.false_match_rate, false)}
    `;
  } else {
    // No baseline for this run: an empty panel is honest, a stale one is not.
    baselineEl.innerHTML = '<div class="empty-state"><div class="es-body">No baseline comparison is available for this run.</div></div>';
  }

  const rows = Object.entries(ev.per_case_type).map(([ct, v]) => {
    const resolvableN = v.CORRECT_AUTO + v.INCORRECT_AUTO + v.MISSED_OPPORTUNITY;
    const unresolvableN = v.UNSAFE_AUTO + v.CORRECTLY_ESCALATED;
    const rate = resolvableN ? v.CORRECT_AUTO / resolvableN : (unresolvableN ? v.CORRECTLY_ESCALATED / unresolvableN : null);
    return { ct, ...v, rate };
  }).sort((a, b) => b.total - a.total);

  const cols = ['case_type', 'total', 'rate', 'CORRECT_AUTO', 'INCORRECT_AUTO', 'MISSED_OPPORTUNITY', 'UNSAFE_AUTO', 'CORRECTLY_ESCALATED'];
  document.querySelector('#caseTypeTable thead').innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
  document.querySelector('#caseTypeTable tbody').innerHTML = rows.map((r) => `
    <tr>
      <td>${escapeHtml(r.ct)}</td><td class="mono">${r.total}</td><td class="mono">${r.rate === null ? '—' : fmtPct(r.rate, 0)}</td>
      <td class="mono" style="color:var(--success)">${r.CORRECT_AUTO}</td>
      <td class="mono" style="color:var(--error)">${r.INCORRECT_AUTO}</td>
      <td class="mono" style="color:var(--warning)">${r.MISSED_OPPORTUNITY}</td>
      <td class="mono" style="color:var(--unsafe)">${r.UNSAFE_AUTO}</td>
      <td class="mono" style="color:var(--info)">${r.CORRECTLY_ESCALATED}</td>
    </tr>`).join('');
}

// ---------------------------------------------------------------------------
// Audit -- rendered as a terminal-style event log
// ---------------------------------------------------------------------------

function logLine(time, event, eventCls, detailHtml) {
  const ic = eventCls === 'accepted' ? 'checkCircle' : eventCls === 'rejected' ? 'xCircle' : 'activity';
  return `<div class="log-line ${eventCls}">
    <div class="log-head">${icon(ic, `log-icon ${eventCls}`)}<span class="log-time">${time}</span><span class="log-event ${eventCls}">${event}</span></div>
    <div class="log-detail">${detailHtml}</div>
  </div>`;
}

// keepLimit=true preserves a page size raised by "Load more". /audit has no offset
// parameter either, so paging raises the limit up to the API cap.
async function refreshAudit(keepLimit = false) {
  if (!state.runId) return;
  if (!keepLimit) state.auditLimit = PAGE_STEP;
  const paymentId = document.getElementById('auditPaymentFilter').value.trim();
  const data = await apiGet('/audit', {
    run_id: state.runId, payment_id: paymentId || undefined, limit: state.auditLimit,
  });
  const entries = [...data.results].reverse(); // API returns newest-first; a log reads oldest-first
  const container = document.getElementById('auditLog');
  const truncated = entries.length < data.total;
  const loadMore = () => {
    state.auditLimit = Math.min(state.auditLimit + PAGE_STEP, API_MAX_LIMIT);
    refreshAudit(true);
  };
  renderPager('auditPager', entries.length, data.total, state.auditLimit >= API_MAX_LIMIT, loadMore);

  if (entries.length === 0 && !paymentId) {
    container.innerHTML = '<div class="empty-state"><div class="es-title">No audit entries</div><div class="es-body">Run reconciliation to generate an audit trail.</div></div>';
    return;
  }
  if (entries.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="es-body">No audit entries for this payment ID.</div></div>';
    return;
  }

  const lines = [];
  const m = runMetrics();
  // The API returns newest-first, so a truncated page is the TAIL of the log: claiming
  // it starts at RECONCILIATION_STARTED would be a lie about what is on screen.
  const framed = !paymentId && hasRunMetrics() && !truncated;
  if (truncated && !paymentId) {
    lines.push(logLine('—', 'LOG_TRUNCATED', 'system',
      `showing the most recent <b>${fmtNum(entries.length)}</b> of <b>${fmtNum(data.total)}</b> entries — load more to reach the start of the batch`));
  }
  if (framed) {
    lines.push(logLine(fmtTime(state.runDetail.started_at), 'RECONCILIATION_STARTED', 'system',
      `batch=<b>${escapeHtml(state.runId)}</b> &nbsp; records=<b>${fmtNum(m.total_payments)}</b>`));
  }
  entries.forEach((a) => {
    const time = fmtTime(a.created_at);
    if (a.actor === 'human_reviewer' || a.status === 'EXCEPTION_REVIEWED') {
      // A human review never changes the engine's decision -- it is its own provenance event.
      lines.push(logLine(time, 'HUMAN_REVIEWED', 'system', `<b>${escapeHtml(a.payment_id)}</b> &nbsp; reason=${escapeHtml(catLabel(a.category))} &nbsp; actor=human_reviewer`));
    } else if (a.status === 'EXCEPTION') {
      lines.push(logLine(time, 'MATCH_REJECTED', 'rejected', `<b>${escapeHtml(a.payment_id)}</b> &nbsp; reason=${escapeHtml(catLabel(a.category))}`));
    } else {
      lines.push(logLine(time, 'MATCH_ACCEPTED', 'accepted', `<b>${escapeHtml(a.payment_id)}</b> &nbsp; confidence=${a.confidence ?? '—'} &nbsp; actor=${escapeHtml(a.actor || '')}`));
    }
  });
  if (framed) {
    const sc = statusCounts();
    const auto = (sc.AUTO_MATCH || 0) + (sc.AI_ASSISTED_MATCH || 0);
    lines.push(logLine(fmtTime(state.runDetail.finished_at), 'BATCH_COMPLETED', 'system',
      `matched=<b>${auto}</b> &nbsp; exceptions=<b>${sc.EXCEPTION || 0}</b>`));
  } else if (!paymentId && !hasRunMetrics()) {
    lines.push(logLine(fmtTime(state.runDetail && state.runDetail.finished_at), `BATCH_${escapeHtml(runStatus())}`, 'rejected',
      `run <b>${escapeHtml(state.runId)}</b> did not report batch totals${state.runDetail && state.runDetail.error ? ` &nbsp; error=${escapeHtml(String(state.runDetail.error))}` : ''}`));
  }
  container.innerHTML = lines.join('');
}
let auditDebounce;
document.getElementById('auditPaymentFilter').addEventListener('input', () => {
  clearTimeout(auditDebounce);
  auditDebounce = setTimeout(() => refreshAudit(), 300);
});

// ---------------------------------------------------------------------------
// dataset generation / reconciliation actions (shared by global controls + hero CTAs)
// ---------------------------------------------------------------------------

// Returns true only when a new dataset actually replaced the previous one. A failed
// generation must never let the caller proceed to reconcile the stale dataset.
async function generateDataset() {
  const btns = [document.getElementById('btnGenerate')];
  const status = document.getElementById('controlStatus');
  btns.forEach((b) => b.disabled = true);
  status.textContent = 'Generating dataset…';
  try {
    const seed = document.getElementById('seedInput').value;
    const size = document.getElementById('sizeInput').value;
    const summary = await apiPost('/dataset/generate', { seed, size });
    status.textContent = `Generated ${summary.payments} payments, ${summary.bank_settlements} bank rows, ${summary.invoices} invoices.`;
    toast('Dataset generated.');
    return true;
  } catch (e) {
    toast(e.message, 'error');
    status.textContent = '';
    return false;
  } finally {
    btns.forEach((b) => b.disabled = false);
  }
}

// Returns the run's metrics on success, or null on failure (having already surfaced
// the error as a toast). Callers must treat a null return as "do not show success".
async function runReconciliation() {
  const btns = [document.getElementById('btnRun'), document.getElementById('btnRunHero')].filter(Boolean);
  const status = document.getElementById('controlStatus');
  btns.forEach((b) => b.disabled = true);
  status.textContent = 'Ingesting, matching, and reasoning over the batch…';
  try {
    if (!(await apiGet('/runs', { limit: 1 }).then(() => true).catch(() => false))) throw new Error('API unreachable.');
    const metrics = await apiPost('/reconcile/run');
    status.textContent = `Run ${metrics.run_id} complete: ${metrics.total_payments} records in ${fmtSeconds(metrics.total_processing_seconds)}`;
    toast('Reconciliation complete.', 'success');
    await loadRuns(metrics.run_id);
    await loadRunData();
    return metrics;
  } catch (e) {
    toast(e.message, 'error');
    status.textContent = '';
    state.lastRunError = e.message;
    return null;
  } finally {
    btns.forEach((b) => b.disabled = false);
  }
}

document.getElementById('btnGenerate').addEventListener('click', generateDataset);
document.getElementById('btnRun').addEventListener('click', runReconciliationWithProgress);
// A failed generation leaves the previous dataset in place: reconcile nothing.
document.getElementById('btnGenerateEmpty').addEventListener('click', async () => {
  if (!(await generateDataset())) return;
  await runReconciliation();
});

// ---------------------------------------------------------------------------
// AI provider settings modal (bring-your-own-key)
// ---------------------------------------------------------------------------

let settingsData = null; // last /settings response (presets + current config)

function openSettingsModal() {
  document.getElementById('settingsOverlay').classList.add('open');
}
function closeSettingsModal() {
  document.getElementById('settingsOverlay').classList.remove('open');
}
document.getElementById('aiStatusPill').addEventListener('click', () => loadSettingsModal());
document.getElementById('settingsClose').addEventListener('click', closeSettingsModal);
document.getElementById('settingsCancel').addEventListener('click', closeSettingsModal);
document.getElementById('settingsOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'settingsOverlay') closeSettingsModal();
});

function populateModelOptions(providerKey) {
  const preset = settingsData.presets[providerKey];
  const modelSelect = document.getElementById('settingsModelSelect');
  const modelCustom = document.getElementById('settingsModelCustom');
  if (!preset || preset.models.length === 0) {
    modelSelect.style.display = 'none';
    modelCustom.style.display = 'block';
    return;
  }
  modelSelect.style.display = 'block';
  modelCustom.style.display = 'none';
  modelSelect.innerHTML = preset.models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('')
    + '<option value="__custom__">Custom model name…</option>';
}

function updateKeyHint(providerKey) {
  // An API key is only valid for the provider that issued it, so the backend clears the stored
  // key on a provider switch (see app/settings.update). Say so before the operator hits Save,
  // otherwise "AI disabled" after switching looks like a bug rather than the intended behavior.
  const hint = document.getElementById('settingsKeyHint');
  const providerChanged = settingsData && providerKey !== settingsData.provider;
  if (providerChanged) {
    hint.textContent = 'Provider changed — enter a new API key to enable AI. '
      + 'The previous provider\'s key does not carry over and will be cleared on save.';
    return;
  }
  hint.textContent = settingsData && settingsData.key_hint
    ? `Currently configured: key ending in ${settingsData.key_hint}. Leave blank to keep it.`
    : 'No API key configured yet -- AI reasoning falls back to explicit exceptions.';
}

function applyProviderPreset(providerKey, keepCurrentValues) {
  const preset = settingsData.presets[providerKey];
  populateModelOptions(providerKey);
  if (!keepCurrentValues) {
    document.getElementById('settingsBaseUrl').value = preset.base_url;
    const modelSelect = document.getElementById('settingsModelSelect');
    if (preset.models.length) modelSelect.value = preset.default_model;
    else document.getElementById('settingsModelCustom').value = preset.default_model;
  }
  const keyLink = document.getElementById('settingsKeyLink');
  if (preset.key_url) {
    keyLink.href = preset.key_url;
    keyLink.style.display = 'inline';
  } else {
    keyLink.style.display = 'none';
  }
  updateKeyHint(providerKey);
}

async function loadSettingsModal() {
  try {
    settingsData = await apiGet('/settings');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  const providerSelect = document.getElementById('settingsProvider');
  providerSelect.innerHTML = Object.entries(settingsData.presets)
    .map(([key, p]) => `<option value="${escapeHtml(key)}">${escapeHtml(p.label)}</option>`).join('');
  providerSelect.value = settingsData.provider;
  applyProviderPreset(settingsData.provider, true);

  // reflect the CURRENT live config, not just the preset defaults
  document.getElementById('settingsBaseUrl').value = settingsData.base_url;
  const modelSelect = document.getElementById('settingsModelSelect');
  if ([...modelSelect.options].some((o) => o.value === settingsData.model)) {
    modelSelect.value = settingsData.model;
  } else if (modelSelect.style.display !== 'none' && modelSelect.options.length) {
    modelSelect.value = '__custom__';
    document.getElementById('settingsModelCustom').style.display = 'block';
    document.getElementById('settingsModelCustom').value = settingsData.model;
  } else {
    document.getElementById('settingsModelCustom').value = settingsData.model;
  }
  document.getElementById('settingsApiKey').value = '';
  updateKeyHint(settingsData.provider);
  openSettingsModal();
}

document.getElementById('settingsProvider').addEventListener('change', (e) => applyProviderPreset(e.target.value, false));
document.getElementById('settingsModelSelect').addEventListener('change', (e) => {
  document.getElementById('settingsModelCustom').style.display = e.target.value === '__custom__' ? 'block' : 'none';
});

document.getElementById('settingsSave').addEventListener('click', async () => {
  const provider = document.getElementById('settingsProvider').value;
  const modelSelect = document.getElementById('settingsModelSelect');
  const model = (modelSelect.style.display !== 'none' && modelSelect.value !== '__custom__')
    ? modelSelect.value
    : document.getElementById('settingsModelCustom').value.trim();
  const baseUrl = document.getElementById('settingsBaseUrl').value.trim();
  const apiKeyInput = document.getElementById('settingsApiKey').value;
  const btn = document.getElementById('settingsSave');
  btn.disabled = true;
  try {
    const params = { provider, base_url: baseUrl, model };
    if (apiKeyInput !== '') params.api_key = apiKeyInput; // omit entirely to keep the existing key
    const updated = await apiPostJson('/settings', params);
    toast(updated.enabled ? `AI enabled: ${updated.provider} / ${updated.model}` : 'Settings saved (no API key set).', 'success');
    closeSettingsModal();
    const health = await apiGet('/health').catch(() => null);
    state.health = health;
    renderAIStatus(health);
    renderSystemStatus(!!health);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

async function bootstrap() {
  authPromptDismissed = false; // a fresh attempt may re-prompt if the API is still 401
  authRequired = false;
  try {
    await loadMetaAndHealth();
    await loadRuns();
    await loadRunData();
  } catch (e) {
    // A 401 means the API is up and refusing us: the auth banner explains that,
    // so do not tell the operator to go restart a healthy backend.
    renderSystemStatus(false, authRequired);
    toast(e.message, 'error');
  }
}
bootstrap();

// ---------------------------------------------------------------------------
// "New reconciliation" modal — staged progress wrapper around the one real
// synchronous /reconcile/run call. Stages are client-timed labels for the
// actual sequential server stages (ingest -> candidates -> scoring -> AI ->
// policy); the moment the real response lands, only real numbers are shown.
// ---------------------------------------------------------------------------

function openReconcileModal() {
  document.getElementById('reconcileConfig').style.display = 'block';
  document.getElementById('reconcileStages').style.display = 'none';
  document.getElementById('reconcileResult').style.display = 'none';
  const aiMode = document.getElementById('reconcileAiMode');
  aiMode.innerHTML = state.health
    ? (state.health.ai_enabled
        ? `AI mode: <b style="color:var(--success)">enabled</b> (${escapeHtml(state.health.llm_model)}) <a href="#" id="reconcileConfigureAi">Configure →</a>`
        : `AI mode: <b style="color:var(--warning)">fallback</b> — ambiguous cases become exceptions <a href="#" id="reconcileConfigureAi">Configure →</a>`)
    : `AI mode: <b>unknown</b> (API unreachable)`;
  const cfgLink = document.getElementById('reconcileConfigureAi');
  if (cfgLink) cfgLink.addEventListener('click', (e) => { e.preventDefault(); loadSettingsModal(); });
  document.getElementById('reconcileOverlay').classList.add('open');
}
function closeReconcileModal() { document.getElementById('reconcileOverlay').classList.remove('open'); }
document.getElementById('btnNewReconciliation').addEventListener('click', openReconcileModal);
document.getElementById('btnRunHero').addEventListener('click', openReconcileModal);
document.getElementById('reconcileClose').addEventListener('click', closeReconcileModal);
document.getElementById('reconcileOverlay').addEventListener('click', (e) => { if (e.target.id === 'reconcileOverlay') closeReconcileModal(); });

const RECONCILE_STAGE_ORDER = ['prepare', 'normalize', 'candidates', 'evidence', 'verify'];
async function runReconciliationWithProgress() {
  document.getElementById('reconcileConfig').style.display = 'none';
  const stagesEl = document.getElementById('reconcileStages');
  stagesEl.style.display = 'flex';
  stagesEl.style.flexDirection = 'column';
  let idx = 0;
  const setStage = (i) => {
    RECONCILE_STAGE_ORDER.forEach((s, si) => {
      const el = stagesEl.querySelector(`[data-stage="${s}"]`);
      el.classList.toggle('done', si < i);
      el.classList.toggle('active', si === i);
    });
  };
  setStage(0);
  const timer = setInterval(() => { idx = Math.min(idx + 1, RECONCILE_STAGE_ORDER.length - 1); setStage(idx); }, 700);
  let metrics = null;
  try {
    metrics = await runReconciliation();
  } finally {
    // The interval is client-side theatre for a server-side sequence: it must stop
    // whether the run succeeded or not, or it keeps advancing stages after failure.
    clearInterval(timer);
  }
  const result = document.getElementById('reconcileResult');
  if (!metrics) {
    // runReconciliation already toasted the error; here we render an explicit failed
    // state instead of leaving the stages "done" and reading stale/absent metrics.
    RECONCILE_STAGE_ORDER.forEach((s) => {
      const el = stagesEl.querySelector(`[data-stage="${s}"]`);
      el.classList.remove('active');
    });
    result.style.display = 'block';
    result.innerHTML = `
      <div class="rr-label" style="color:var(--error)">reconciliation failed</div>
      <div class="rr-total" style="color:var(--error)">—</div>
      <div class="rr-split">
        <div class="rr-col review"><b>—</b><span>no records were analyzed</span></div>
      </div>
      <div class="muted" style="font-size:13px;max-width:420px;margin:var(--sp-4) auto 0">${escapeHtml(state.lastRunError || 'The reconciliation run did not complete.')}</div>
      <div style="display:flex;gap:8px;justify-content:center;margin-top:var(--sp-6)">
        <button class="btn" id="btnReconcileRetry">Try again</button>
        <button class="btn btn-primary" id="btnReconcileClose">Close</button>
      </div>`;
    document.getElementById('btnReconcileClose').addEventListener('click', closeReconcileModal);
    document.getElementById('btnReconcileRetry').addEventListener('click', () => {
      result.style.display = 'none';
      openReconcileModal();
    });
    return;
  }
  RECONCILE_STAGE_ORDER.forEach((s) => stagesEl.querySelector(`[data-stage="${s}"]`).classList.add('done', 'active'));
  const sc = metrics.status_counts || {};
  const total = metrics.total_payments;
  const reconciled = (sc.AUTO_MATCH || 0) + (sc.AI_ASSISTED_MATCH || 0);
  result.style.display = 'block';
  result.innerHTML = `
    <div class="rr-label">records analyzed</div>
    <div class="rr-total">${fmtNum(total)}</div>
    <div class="rr-split">
      <div class="rr-col safe"><b>${fmtNum(reconciled)}</b><span>safe to automate</span></div>
      <div class="rr-col review"><b>${fmtNum(sc.EXCEPTION || 0)}</b><span>require review</span></div>
    </div>
    <button class="btn btn-primary" style="margin-top:var(--sp-6)" id="btnReconcileDone">View results</button>`;
  document.getElementById('btnReconcileDone').addEventListener('click', () => { closeReconcileModal(); switchView('overview'); });
}

// ---------------------------------------------------------------------------
// Landing scrollytelling -- four real-data "moments" driven by scroll
// progress within tall pinned sections. Framework-free: one rAF loop reads
// each .scrolly section's position and calls its registered updater.
// ---------------------------------------------------------------------------

const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (REDUCE_MOTION) document.body.classList.add('no-scrolly');

function scrollyProgress(section) {
  const rect = section.getBoundingClientRect();
  const total = rect.height - window.innerHeight;
  if (total <= 0) return 1;
  return Math.min(1, Math.max(0, -rect.top / total));
}

const MOMENT_UPDATERS = {};

function tickScrolly() {
  if (!REDUCE_MOTION && !document.getElementById('landing').classList.contains('hidden')) {
    document.querySelectorAll('.scrolly').forEach((section) => {
      const fn = MOMENT_UPDATERS[section.id];
      if (fn) fn(scrollyProgress(section));
    });
  }
  requestAnimationFrame(tickScrolly);
}
if (!REDUCE_MOTION) requestAnimationFrame(tickScrolly);

MOMENT_UPDATERS['moment-split'] = (p) => {
  const introEl = document.getElementById('momentSplitIntro');
  const resultEl = document.getElementById('momentSplitResult');
  const total = hasRunMetrics() ? runMetrics().total_payments : null;
  if (total === null) { introEl.classList.add('in'); resultEl.classList.remove('in'); document.getElementById('momentSplitTotal').textContent = '—'; return; }
  const sc = statusCounts();
  const reconciled = (sc.AUTO_MATCH || 0) + (sc.AI_ASSISTED_MATCH || 0);
  const exceptions = sc.EXCEPTION || 0;
  introEl.classList.toggle('in', p < 0.55);
  resultEl.classList.toggle('in', p >= 0.45);
  document.getElementById('momentSplitTotal').textContent = fmtNum(total);
  const localP = Math.min(1, Math.max(0, (p - 0.5) / 0.5));
  document.getElementById('momentSplitSafe').textContent = fmtNum(Math.round(reconciled * localP));
  document.getElementById('momentSplitReview').textContent = fmtNum(Math.round(exceptions * localP));
};

async function loadStoryExamples() {
  if (!state.recentPreview || state.recentPreview.length === 0) { state.storyExample = null; return; }
  const matchedRow = state.recentPreview.find((d) => d.status !== 'EXCEPTION');
  const exceptionRow = state.recentPreview.find((d) => d.status === 'EXCEPTION');
  const [matched, exception] = await Promise.all([
    matchedRow ? apiGet(`/decisions/${encodeURIComponent(matchedRow.payment_id)}`, { run_id: state.runId }) : null,
    exceptionRow ? apiGet(`/decisions/${encodeURIComponent(exceptionRow.payment_id)}`, { run_id: state.runId }) : null,
  ]);
  state.storyExample = { matched, exception };
  renderMomentEvidence();
  renderMomentException();
}

function renderMomentEvidence() {
  const ex = state.storyExample && state.storyExample.matched;
  if (!ex) return;
  const { payment, decision } = ex;
  const candidate = extractCandidate(decision);
  document.getElementById('momentEvidenceTxn').innerHTML = `<div class="mtc-id">${escapeHtml(decision.payment_id || payment.payment_id)}</div><div class="mtc-amount">${fmtAmount(payment.amount)}</div>`;
  document.getElementById('momentEvidenceRows').innerHTML = renderChecklist(candidate) + renderVerdict(decision);
  document.querySelectorAll('#momentEvidenceRows .check-row, #momentEvidenceRows .verdict-box').forEach((el, i) => {
    el.classList.add('moment-evidence-row');
    el.dataset.revealIndex = i;
  });
}
MOMENT_UPDATERS['moment-evidence'] = (p) => {
  const rows = document.querySelectorAll('#momentEvidenceRows .moment-evidence-row');
  const n = rows.length || 1;
  rows.forEach((el) => {
    const threshold = Number(el.dataset.revealIndex) / n;
    el.classList.toggle('in', p >= threshold);
  });
};

function renderMomentException() {
  const ex = state.storyExample && state.storyExample.exception;
  if (!ex) return;
  const { payment, decision } = ex;
  const candidate = extractCandidate(decision);
  document.getElementById('momentExceptionTxn').innerHTML = `<div class="mtc-id">${escapeHtml(decision.payment_id || payment.payment_id)}</div><div class="mtc-amount">${fmtAmount(payment.amount)}</div>`;
  document.getElementById('momentExceptionRows').innerHTML = renderChecklist(candidate) + renderVerdict(decision);
  document.querySelectorAll('#momentExceptionRows .check-row, #momentExceptionRows .verdict-box').forEach((el, i) => {
    el.classList.add('moment-evidence-row');
    el.dataset.revealIndex = i;
  });
}
MOMENT_UPDATERS['moment-exception'] = (p) => {
  const rows = document.querySelectorAll('#momentExceptionRows .moment-evidence-row');
  const n = rows.length || 1;
  rows.forEach((el) => { el.classList.toggle('in', p >= Number(el.dataset.revealIndex) / n); });
};

MOMENT_UPDATERS['moment-evaluation'] = (p) => {
  const ev = state.evaluation;
  if (!ev) return;
  document.getElementById('momentEvalPrecision').textContent = fmtPct(ev.automation_precision * p, 1);
  document.getElementById('momentEvalSafety').textContent = fmtPct(ev.safety_rate * p, 1);
  document.getElementById('momentEvalFalse').textContent = p > 0.2 ? fmtPct(ev.false_match_rate, 2) : '—';
};
document.getElementById('momentEvalCta').addEventListener('click', (e) => { e.preventDefault(); enterApp(); switchView('evaluation'); });
