// LedgerProof frontend — vanilla JS, no build step. Talks to the FastAPI
// backend over fetch(). Every render function is pure: takes data,
// returns/mutates DOM, never hides a failure silently (network/API errors
// surface as a toast or a persistent banner, not a blank screen).
'use strict';

const API_BASE = (() => {
  const stored = localStorage.getItem('ledgerproof_api_base');
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

const state = {
  meta: null, health: null, runs: [], runId: null, runDetail: null,
  evaluation: null, baseline: null, charts: {}, excCategory: '',
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

async function request(method, path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''));
  const url = `${API_BASE}${path}${qs.toString() ? '?' + qs.toString() : ''}`;
  setLoading(true);
  try {
    const res = await fetch(url, { method });
    const body = await res.json().catch(() => ({}));
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
// ---------------------------------------------------------------------------

Chart.defaults.color = '#A8A49B';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.borderColor = '#26251F';
// Every chart lives in a `.chart-box` with an explicit CSS height. Without
// this, Chart.js sizes the canvas from its own aspect ratio based on
// container width and ignores that height, overflowing past the panel.
Chart.defaults.maintainAspectRatio = false;
Chart.defaults.responsive = true;

function upsertChart(key, canvasId, config) {
  if (state.charts[key]) state.charts[key].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  state.charts[key] = new Chart(ctx, config);
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
document.getElementById('btnEnter').addEventListener('click', enterApp);
document.getElementById('btnEnterFinal').addEventListener('click', enterApp);
function updateLandingStats() {
  const total = state.runDetail ? state.runDetail.metrics.total_payments : null;
  document.getElementById('landingStatusLine').innerHTML = total
    ? `<span class="stat-chip">${icon('database')}<b>${fmtNum(total)}</b> records</span><span class="stat-chip">${icon('activity')}latest run <b>${state.runId}</b></span><span class="stat-chip">${icon('clock')}<b>${state.runDetail.metrics.total_processing_seconds.toFixed(2)}s</b> to process</span>`
    : '';

  // hero mockup preview -- a live miniature of the real Overview, not a
  // decorative fake chart. Reuses the same run metrics rendered elsewhere.
  const kpis = document.querySelectorAll('#heroPreviewBody .hpv-kpi .v');
  if (state.runDetail && kpis.length === 2) {
    const m = state.runDetail.metrics;
    const sc = m.status_counts;
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
      rows[i].innerHTML = `<span class="d" style="background:${ok ? 'var(--success)' : 'var(--error)'}"></span>${d.payment_id} ${ok ? 'auto-matched' : 'exception'}`;
    });
  }
}

// ---------------------------------------------------------------------------
// sidebar: collapse (desktop) + off-canvas (mobile)
// ---------------------------------------------------------------------------

const SIDEBAR_KEY = 'ledgerproof_sidebar_collapsed';
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

function renderSystemStatus(ok) {
  const block = document.getElementById('systemStatusBlock');
  const banner = document.getElementById('apiOfflineBanner');
  block.classList.toggle('offline', !ok);
  document.getElementById('systemStatusText').textContent = ok ? 'SYSTEM OPERATIONAL' : 'API UNREACHABLE';
  banner.classList.toggle('show', !ok);
  document.getElementById('statusRule').textContent = ok ? 'ACTIVE' : 'OFFLINE';
  document.getElementById('statusRule').className = 'system-row-status ' + (ok ? 'on' : 'off');
  document.getElementById('statusPipeline').textContent = ok ? 'READY' : 'OFFLINE';
  document.getElementById('statusPipeline').className = 'system-row-status ' + (ok ? 'on' : 'off');
}

function updateLastRunLabel() {
  const el = document.getElementById('systemLastRun');
  if (state.runDetail) {
    el.textContent = `Last run: ${fmtTime(state.runDetail.finished_at || state.runDetail.started_at)}`;
  } else {
    el.textContent = 'No runs yet';
  }
}

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
  Object.entries(meta.statuses).forEach(([k, v]) => statusSel.insertAdjacentHTML('beforeend', `<option value="${k}">${v}</option>`));
  const catSel = document.getElementById('filterCategory');
  catSel.querySelectorAll('option:not(:first-child)').forEach((o) => o.remove());
  Object.entries(meta.category_labels).forEach(([k, v]) => catSel.insertAdjacentHTML('beforeend', `<option value="${k}">${v}</option>`));
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
    sel.insertAdjacentHTML('beforeend', `<option value="${r.run_id}">${r.run_id} (${r.total_payments} records)</option>`);
  });
  sel.value = selectRunId && state.runs.some((r) => r.run_id === selectRunId) ? selectRunId : state.runs[0].run_id;
  state.runId = sel.value;
}

document.getElementById('runSelect').addEventListener('change', (e) => {
  state.runId = e.target.value;
  loadRunData();
});

function setOverviewEmpty(isEmpty) {
  document.getElementById('overviewEmpty').style.display = isEmpty ? 'block' : 'none';
  document.getElementById('overviewData').style.display = isEmpty ? 'none' : 'block';
}

async function loadRunData() {
  if (!state.runId) {
    document.getElementById('overviewCaption').textContent = 'Evidence-driven reconciliation for merchant finance operations.';
    document.getElementById('overviewStatusLine').innerHTML = '';
    setOverviewEmpty(true);
    updateLastRunLabel();
    updateLandingStats();
    return;
  }
  try {
    const [detail, evaluation, baseline] = await Promise.all([
      apiGet(`/runs/${state.runId}`),
      apiGet(`/runs/${state.runId}/evaluation`).catch(() => null),
      apiGet('/baseline').catch(() => null),
    ]);
    state.runDetail = detail;
    state.evaluation = evaluation;
    state.baseline = baseline;
    setOverviewEmpty(false);
    renderOverview();
    renderEvaluation();
    updateLandingStats();
    updateLastRunLabel();
    await Promise.all([refreshDecisions(), refreshExceptions(), refreshAudit(), refreshRecent()]);
    updateLandingStats();
    loadStoryExamples();
  } catch (e) {
    toast(e.message, 'error', e && e.stack);
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

function renderOverview() {
  const m = state.runDetail.metrics;
  const sc = m.status_counts;
  const total = m.total_payments;
  const auto = sc.AUTO_MATCH || 0, ai = sc.AI_ASSISTED_MATCH || 0, exc = sc.EXCEPTION || 0;
  const reconciled = auto + ai;
  const reconciliationRate = total ? reconciled / total : null;

  document.getElementById('overviewCaption').textContent = 'Evidence-driven reconciliation for merchant finance operations.';
  document.getElementById('overviewStatusLine').innerHTML =
    `<span class="stat-chip">${icon('database')}<b>${fmtNum(total)}</b> records</span><span class="stat-chip">${icon('activity')}run <b class="mono">${state.runId}</b></span><span class="stat-chip">${icon('clock')}<b>${m.total_processing_seconds.toFixed(2)}s</b> to process</span>`;

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
    slimStat('processing time', `${m.total_processing_seconds.toFixed(2)}s`, 'clock'),
    slimStat('avg / record', m.avg_processing_ms_per_record ? `${m.avg_processing_ms_per_record.toFixed(2)}ms` : '—', 'clock'),
    slimStat('AI invocations', fmtNum(m.ai_invocations), 'cpu'),
  ].join('');

  const proof = document.getElementById('proofStrip');
  if (state.evaluation && state.baseline) {
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
  document.getElementById('propBar').innerHTML = segs.map((s) => {
    const pct = (s.n / total) * 100;
    return `<div class="prop-seg ${s.key}" style="width:${pct}%">${pct >= 7 ? fmtPct(s.n / total, 0) : ''}</div>`;
  }).join('');
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
        <span class="rl-label">${catLabel(k)}</span>
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
    <tr data-payment="${d.payment_id}" tabindex="0">
      <td class="mono">${d.payment_id}</td>
      <td class="mono">${fmtAmount(d.amount)}</td>
      <td>${statusBadge(d.status)}</td>
    </tr>`).join('');
  tbody.querySelectorAll('tr').forEach((row) => row.addEventListener('click', () => openDecisionDrawer(row.dataset.payment)));
}

// ---------------------------------------------------------------------------
// Reconciliation (decisions table)
// ---------------------------------------------------------------------------

async function refreshDecisions() {
  if (!state.runId) return;
  const status = document.getElementById('filterStatus').value;
  const category = document.getElementById('filterCategory').value;
  const data = await apiGet('/decisions', { run_id: state.runId, status, category, limit: 300 });
  document.getElementById('decisionsCount').textContent = `${data.results.length} of ${data.total} shown`;
  const m = state.runDetail.metrics;
  document.getElementById('reconCaption').innerHTML =
    `${fmtNum(m.total_payments)} records &nbsp;·&nbsp; <span style="color:var(--success)">${fmtNum((m.status_counts.AUTO_MATCH||0)+(m.status_counts.AI_ASSISTED_MATCH||0))} automatically reconciled</span> &nbsp;·&nbsp; <span style="color:var(--error)">${fmtNum(m.status_counts.EXCEPTION||0)} sent for review</span>`;

  const cols = ['payment_id', 'customer_name', 'amount', 'status', 'category', 'matched_bank_ref', 'confidence', 'method', 'ai_used'];
  document.querySelector('#decisionsTable thead').innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
  const tbody = document.querySelector('#decisionsTable tbody');
  if (data.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state"><div class="es-title">No matching records</div><div class="es-body">No decisions match this filter combination.</div></div></td></tr>`;
    return;
  }
  tbody.innerHTML = data.results.map((d) => `
    <tr data-payment="${d.payment_id}" tabindex="0">
      <td class="mono">${d.payment_id}</td>
      <td>${escapeHtml(d.customer_name || '')}</td>
      <td class="mono">${fmtAmount(d.amount)}</td>
      <td>${statusBadge(d.status)}</td>
      <td class="muted">${catLabel(d.category)}</td>
      <td class="mono muted">${d.matched_bank_ref || '—'}</td>
      <td class="mono">${d.confidence ?? '—'}</td>
      <td class="muted">${d.method}</td>
      <td>${d.ai_used ? 'yes' : 'no'}</td>
    </tr>`).join('');
  tbody.querySelectorAll('tr').forEach((row) => row.addEventListener('click', () => openDecisionDrawer(row.dataset.payment)));
}
document.getElementById('filterStatus').addEventListener('change', refreshDecisions);
document.getElementById('filterCategory').addEventListener('change', refreshDecisions);

async function openDecisionDrawer(paymentId) {
  try {
    const detail = await apiGet(`/decisions/${paymentId}`, { run_id: state.runId });
    const { payment, decision, audit_trail, exception } = detail;
    const candidate = extractCandidate(decision);
    document.getElementById('drawerTitle').textContent = paymentId;
    const candidateBit = decision.invoice_id ? ` — candidate ${escapeHtml(decision.invoice_id)}` : '';
    document.getElementById('drawerSubtitle').innerHTML = `${escapeHtml(payment?.customer_name || '')} — ${fmtAmount(payment?.amount)}${candidateBit}`;

    let resolveButtonHtml = '';
    if (exception) {
      resolveButtonHtml = exception.resolved
        ? `<span class="badge resolved" style="margin-top:14px">Marked resolved</span>`
        : `<button class="btn btn-danger-outline" id="btnResolve" style="margin-top:14px" data-exc="${exception.id}">Resolve manually</button>`;
    }

    const confidencePct = decision.confidence !== null && decision.confidence !== undefined
      ? `<div class="confidence-block"><span class="cv" style="color:${decision.status === 'EXCEPTION' ? 'var(--error)' : 'var(--success)'}">${decision.confidence}%</span><span class="cl">confidence</span></div>` : '';

    document.getElementById('drawerBody').innerHTML = `
      <div>${statusBadge(decision.status)} ${decision.category ? `<span class="badge exception" style="margin-left:6px">${catLabel(decision.category)}</span>` : ''}</div>
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
    await apiPost(`/exceptions/${exceptionId}/resolve`);
    if (btnEl) btnEl.outerHTML = `<span class="badge resolved" style="margin-top:14px">Marked resolved</span>`;
    toast('Exception marked as reviewed.', 'success');
    refreshExceptions();
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

async function refreshExceptions() {
  if (!state.runId) return;
  const m = state.runDetail.metrics;
  const total = m.status_counts.EXCEPTION || 0;
  document.getElementById('excCount').textContent = total || '';
  document.getElementById('excHeaderCount').textContent = total === 0
    ? 'All records were reconciled safely.'
    : `${fmtNum(total)} record${total === 1 ? '' : 's'} require attention.`;

  // summary strip: total + top categories, all from real category_counts
  const cc = Object.entries(m.category_counts || {}).sort((a, b) => b[1] - a[1]);
  const summaryCards = [heroKpi('Total exceptions', fmtNum(total), total > 0 ? 'error' : '', 'alertTriangle', total > 0 ? 'error' : 'neutral')]
    .concat(cc.slice(0, 3).map(([k, v]) => heroKpi(catLabel(k), fmtNum(v), '', 'clipboardList', 'neutral')));
  document.getElementById('excSummary').innerHTML = summaryCards.join('');

  // filter chips, built from real categories present in meta + counts from this run
  const chipEntries = [['', 'All', total]].concat(cc.map(([k, v]) => [k, catLabel(k), v]));
  buildChips(document.getElementById('excFilterChips'), chipEntries, state.excCategory, (val) => {
    state.excCategory = val;
    refreshExceptions();
  });

  const data = await apiGet('/exceptions', { run_id: state.runId, category: state.excCategory, limit: 300 });
  const list = document.getElementById('exceptionsList');
  if (data.results.length === 0) {
    list.innerHTML = `<div class="empty-state"><div class="es-title">No exceptions</div><div class="es-body">${total === 0 ? 'All records in this run were reconciled safely.' : 'No exceptions match this filter.'}</div></div>`;
    return;
  }
  list.innerHTML = data.results.map((r, i) => {
    const ev = r.evidence || {};
    const candidate = (ev.evidence_found && ev.evidence_found.candidates_considered && ev.evidence_found.candidates_considered[0]) || null;
    return `<div class="exc-card ${r.resolved ? 'is-resolved' : ''}" id="exc-${i}">
      <div class="exc-head" onclick="document.getElementById('exc-${i}').classList.toggle('open')">
        ${icon('alertTriangle', 'exc-icon')}
        <span class="chev">&#9656;</span>
        <span class="exc-title"><span class="pid">${r.payment_id}</span> — ${escapeHtml(r.customer_name || '')} — ${fmtAmount(r.amount)}</span>
        ${r.resolved ? '<span class="badge resolved">Resolved</span>' : `<span class="badge exception">${icon('alertTriangle', 'badge-icon')}${catLabel(r.category)}</span>`}
      </div>
      <div class="exc-body"><div class="exc-body-inner">
        <h4 style="margin-top:0">Evidence checklist</h4>
        ${renderChecklist(candidate)}
        <dl>
          <dt>Why unresolved</dt><dd>${escapeHtml(ev.why_unresolved || r.reason || '')}</dd>
          <dt>Suggested next action</dt><dd>${escapeHtml(r.suggested_action || '')}</dd>
        </dl>
        ${r.resolved ? '' : `<button class="btn btn-danger-outline" data-exc="${r.id}" onclick="resolveException(${r.id}, this)">Resolve manually</button>`}
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

function renderEvaluation() {
  const ev = state.evaluation;
  if (!ev) return;
  const o = ev.outcomes;

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

  if (state.baseline) {
    const b = state.baseline;
    document.getElementById('baselineCompare').innerHTML = `
      <div class="cmp-legend"><span><span class="sw" style="background:var(--success)"></span>LedgerProof</span><span><span class="sw" style="background:var(--error)"></span>Naive baseline</span></div>
      ${cmpRow('Precision', ev.automation_precision, b.automation_precision)}
      ${cmpRow('Coverage', ev.coverage_recall, b.coverage_recall)}
      ${cmpRow('Safety rate', ev.safety_rate, b.safety_rate)}
      ${cmpRow('False-match rate', ev.false_match_rate, b.false_match_rate, false)}
    `;
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
      <td>${r.ct}</td><td class="mono">${r.total}</td><td class="mono">${r.rate === null ? '—' : fmtPct(r.rate, 0)}</td>
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

async function refreshAudit() {
  if (!state.runId) return;
  const paymentId = document.getElementById('auditPaymentFilter').value.trim();
  const data = await apiGet('/audit', { run_id: state.runId, payment_id: paymentId || undefined, limit: 300 });
  const entries = [...data.results].reverse(); // API returns newest-first; a log reads oldest-first
  const container = document.getElementById('auditLog');

  if (entries.length === 0 && !paymentId) {
    container.innerHTML = '<div class="empty-state"><div class="es-title">No audit entries</div><div class="es-body">Run reconciliation to generate an audit trail.</div></div>';
    return;
  }
  if (entries.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="es-body">No audit entries for this payment ID.</div></div>';
    return;
  }

  const lines = [];
  const m = state.runDetail && state.runDetail.metrics;
  if (!paymentId && m) {
    lines.push(logLine(fmtTime(state.runDetail.started_at), 'RECONCILIATION_STARTED', 'system',
      `batch=<b>${state.runId}</b> &nbsp; records=<b>${m.total_payments}</b>`));
  }
  entries.forEach((a) => {
    const time = fmtTime(a.created_at);
    if (a.status === 'EXCEPTION') {
      lines.push(logLine(time, 'MATCH_REJECTED', 'rejected', `<b>${a.payment_id}</b> &nbsp; reason=${catLabel(a.category)}`));
    } else {
      lines.push(logLine(time, 'MATCH_ACCEPTED', 'accepted', `<b>${a.payment_id}</b> &nbsp; confidence=${a.confidence ?? '—'} &nbsp; actor=${a.actor}`));
    }
  });
  if (!paymentId && m) {
    const auto = (m.status_counts.AUTO_MATCH || 0) + (m.status_counts.AI_ASSISTED_MATCH || 0);
    lines.push(logLine(fmtTime(state.runDetail.finished_at), 'BATCH_COMPLETED', 'system',
      `matched=<b>${auto}</b> &nbsp; exceptions=<b>${m.status_counts.EXCEPTION || 0}</b>`));
  }
  container.innerHTML = lines.join('');
}
let auditDebounce;
document.getElementById('auditPaymentFilter').addEventListener('input', () => {
  clearTimeout(auditDebounce);
  auditDebounce = setTimeout(refreshAudit, 300);
});

// ---------------------------------------------------------------------------
// dataset generation / reconciliation actions (shared by global controls + hero CTAs)
// ---------------------------------------------------------------------------

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
  } catch (e) {
    toast(e.message, 'error');
    status.textContent = '';
  } finally {
    btns.forEach((b) => b.disabled = false);
  }
}

async function runReconciliation() {
  const btns = [document.getElementById('btnRun'), document.getElementById('btnRunHero')].filter(Boolean);
  const status = document.getElementById('controlStatus');
  btns.forEach((b) => b.disabled = true);
  status.textContent = 'Ingesting, matching, and reasoning over the batch…';
  try {
    if (!(await apiGet('/runs', { limit: 1 }).then(() => true).catch(() => false))) throw new Error('API unreachable.');
    const metrics = await apiPost('/reconcile/run');
    status.textContent = `Run ${metrics.run_id} complete: ${metrics.total_payments} records in ${metrics.total_processing_seconds}s`;
    toast('Reconciliation complete.', 'success');
    await loadRuns(metrics.run_id);
    await loadRunData();
  } catch (e) {
    toast(e.message, 'error');
    status.textContent = '';
  } finally {
    btns.forEach((b) => b.disabled = false);
  }
}

document.getElementById('btnGenerate').addEventListener('click', generateDataset);
document.getElementById('btnRun').addEventListener('click', runReconciliationWithProgress);
document.getElementById('btnGenerateEmpty').addEventListener('click', async () => { await generateDataset(); await runReconciliation(); });

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
  modelSelect.innerHTML = preset.models.map((m) => `<option value="${m}">${m}</option>`).join('')
    + '<option value="__custom__">Custom model name…</option>';
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
    .map(([key, p]) => `<option value="${key}">${p.label}</option>`).join('');
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
  document.getElementById('settingsKeyHint').textContent = settingsData.key_hint
    ? `Currently configured: key ending in ${settingsData.key_hint}. Leave blank to keep it.`
    : 'No API key configured yet -- AI reasoning falls back to explicit exceptions.';
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
    const updated = await apiPost('/settings', params);
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
  try {
    await loadMetaAndHealth();
    await loadRuns();
    await loadRunData();
  } catch (e) {
    renderSystemStatus(false);
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
  try {
    await runReconciliation();
    clearInterval(timer);
    RECONCILE_STAGE_ORDER.forEach((s) => stagesEl.querySelector(`[data-stage="${s}"]`).classList.add('done', 'active'));
    const m = state.runDetail.metrics;
    const sc = m.status_counts;
    const reconciled = (sc.AUTO_MATCH || 0) + (sc.AI_ASSISTED_MATCH || 0);
    const result = document.getElementById('reconcileResult');
    result.style.display = 'block';
    result.innerHTML = `
      <div class="rr-label">records analyzed</div>
      <div class="rr-total">${fmtNum(m.total_payments)}</div>
      <div class="rr-split">
        <div class="rr-col safe"><b>${fmtNum(reconciled)}</b><span>safe to automate</span></div>
        <div class="rr-col review"><b>${fmtNum(sc.EXCEPTION || 0)}</b><span>require review</span></div>
      </div>
      <button class="btn btn-primary" style="margin-top:var(--sp-6)" id="btnReconcileDone">View results</button>`;
    document.getElementById('btnReconcileDone').addEventListener('click', () => { closeReconcileModal(); switchView('overview'); });
  } catch (e) {
    clearInterval(timer);
    closeReconcileModal();
  }
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
  const total = state.runDetail ? state.runDetail.metrics.total_payments : null;
  if (total === null) { introEl.classList.add('in'); resultEl.classList.remove('in'); document.getElementById('momentSplitTotal').textContent = '—'; return; }
  const sc = state.runDetail.metrics.status_counts;
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
    matchedRow ? apiGet(`/decisions/${matchedRow.payment_id}`, { run_id: state.runId }) : null,
    exceptionRow ? apiGet(`/decisions/${exceptionRow.payment_id}`, { run_id: state.runId }) : null,
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
  document.getElementById('momentEvidenceTxn').innerHTML = `<div class="mtc-id">${decision.payment_id || payment.payment_id}</div><div class="mtc-amount">${fmtAmount(payment.amount)}</div>`;
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
  document.getElementById('momentExceptionTxn').innerHTML = `<div class="mtc-id">${decision.payment_id || payment.payment_id}</div><div class="mtc-amount">${fmtAmount(payment.amount)}</div>`;
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
