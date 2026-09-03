// Veyra frontend — vanilla JS, no build step. Talks to the FastAPI backend
// over fetch(). Every render function is pure: takes data, returns/mutates
// DOM, never hides a failure silently (network/API errors surface as a
// toast, not a blank screen).
'use strict';

const API_BASE = (() => {
  const stored = localStorage.getItem('veyra_api_base');
  if (stored) return stored;
  return `${location.protocol}//${location.hostname}:8000`;
})();

const STATUS_COLORS = { AUTO_MATCH: '#3ecf8e', AI_ASSISTED_MATCH: '#6ea8ea', EXCEPTION: '#ec6b6b' };
const OUTCOME_COLORS = {
  CORRECT_AUTO: '#3ecf8e', INCORRECT_AUTO: '#ec6b6b', MISSED_OPPORTUNITY: '#e0b04a',
  UNSAFE_AUTO: '#8b2f2f', CORRECTLY_ESCALATED: '#6ea8ea',
};
const VIEW_TITLES = {
  overview: 'Overview', reconciliation: 'Reconciliation', exceptions: 'Exceptions',
  evaluation: 'Evaluation', audit: 'Audit Trail',
};

const state = {
  meta: null, health: null, runs: [], runId: null, runDetail: null,
  evaluation: null, baseline: null, charts: {},
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
const catLabel = (c) => (state.meta && c && state.meta.category_labels[c]) || c || '—';
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function statusBadge(status) {
  const cls = status === 'AUTO_MATCH' ? 'auto' : status === 'AI_ASSISTED_MATCH' ? 'ai' : 'exception';
  const label = (state.meta && state.meta.statuses[status]) || status;
  return `<span class="badge ${cls}"><span class="badge-dot"></span>${escapeHtml(label)}</span>`;
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

function extractCandidate(decision) {
  const ev = decision.evidence || {};
  if (decision.status === 'EXCEPTION') {
    const list = (ev.evidence_found && ev.evidence_found.candidates_considered) || [];
    return list[0] || null;
  }
  return ev.matched_candidate || null;
}

function renderChecklist(c) {
  if (!c) return '<div class="empty-state">No candidate bank record was found to evaluate.</div>';
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

Chart.defaults.color = '#98979f';
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.borderColor = '#29292f';
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
// landing -> app
// ---------------------------------------------------------------------------

document.getElementById('btnEnter').addEventListener('click', () => {
  document.getElementById('landing').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
});

function updateLandingStats() {
  const stats = document.querySelectorAll('#landingStats .stat b');
  if (!state.runDetail || !stats.length) return;
  const total = state.runDetail.metrics.total_payments;
  const precision = state.evaluation ? fmtPct(state.evaluation.automation_precision, 1) : '—';
  const falseRate = state.evaluation ? fmtPct(state.evaluation.false_match_rate, 1) : '—';
  stats[0].textContent = fmtNum(total);
  stats[1].textContent = precision;
  stats[2].textContent = falseRate;
}

// ---------------------------------------------------------------------------
// sidebar nav
// ---------------------------------------------------------------------------

function switchView(name) {
  document.querySelectorAll('.nav-item[data-view]').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  document.getElementById('topbarTitle').textContent = VIEW_TITLES[name] || name;
}
document.querySelectorAll('.nav-item[data-view]').forEach((item) => {
  item.addEventListener('click', () => switchView(item.dataset.view));
});
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-goto]');
  if (link) { e.preventDefault(); switchView(link.dataset.goto); }
});

// ---------------------------------------------------------------------------
// bootstrap
// ---------------------------------------------------------------------------

async function loadMetaAndHealth() {
  const [meta, health] = await Promise.all([apiGet('/meta'), apiGet('/health').catch(() => null)]);
  state.meta = meta;
  state.health = health;
  const pill = document.getElementById('aiStatusPill');
  const text = document.getElementById('aiStatusText');
  const dotAI = document.getElementById('dotAI');
  if (!health) {
    pill.className = 'status-pill off';
    text.textContent = 'API unreachable';
    dotAI.className = 'system-dot off';
  } else if (health.ai_enabled) {
    pill.className = 'status-pill on';
    text.textContent = `AI enabled (${health.llm_model})`;
    dotAI.className = 'system-dot on';
  } else {
    pill.className = 'status-pill off';
    text.textContent = 'AI disabled — fallback mode';
    dotAI.className = 'system-dot off';
  }
  document.getElementById('apiTarget').textContent = API_BASE;

  const statusSel = document.getElementById('filterStatus');
  Object.entries(meta.statuses).forEach(([k, v]) => statusSel.insertAdjacentHTML('beforeend', `<option value="${k}">${v}</option>`));
  const catSelectors = [document.getElementById('filterCategory'), document.getElementById('excFilterCategory')];
  catSelectors.forEach((sel) => {
    Object.entries(meta.category_labels).forEach(([k, v]) => sel.insertAdjacentHTML('beforeend', `<option value="${k}">${v}</option>`));
  });
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

async function loadRunData() {
  if (!state.runId) {
    document.getElementById('overviewCaption').textContent = 'No reconciliation run yet. Generate a dataset and click "Run reconciliation" above.';
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
    renderOverview();
    renderEvaluation();
    updateLandingStats();
    await Promise.all([refreshDecisions(), refreshExceptions(), refreshAudit(), refreshRecent()]);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

function heroKpi(label, value, cls = '') {
  return `<div class="hero-kpi"><div class="hero-kpi-label">${label}</div><div class="hero-kpi-value ${cls}">${value}</div></div>`;
}
function slimStat(label, value) {
  return `<div class="slim-stat"><span class="v">${value}</span><span class="l">${label}</span></div>`;
}

function renderOverview() {
  const m = state.runDetail.metrics;
  const ev = state.evaluation;
  const sc = m.status_counts;
  const total = m.total_payments;
  const auto = sc.AUTO_MATCH || 0, ai = sc.AI_ASSISTED_MATCH || 0, exc = sc.EXCEPTION || 0;

  document.getElementById('overviewCaption').innerHTML =
    `Evidence-driven reconciliation for merchant finance operations. &nbsp;—&nbsp; run <span class="mono">${state.runId}</span>, started ${new Date(state.runDetail.started_at).toLocaleString()}`;

  document.getElementById('heroKpis').innerHTML = [
    heroKpi('Records processed', fmtNum(total)),
    heroKpi('Automatically reconciled', fmtNum(auto + ai), 'emerald'),
    heroKpi('Sent for review', fmtNum(exc), exc > 0 ? 'red' : ''),
    heroKpi('Safety rate', ev ? fmtPct(ev.safety_rate) : fmtPct((auto + ai) / total), 'brass'),
  ].join('');

  document.getElementById('slimStats').innerHTML = [
    slimStat('throughput', m.throughput_per_second ? `${m.throughput_per_second.toFixed(1)}/s` : '—'),
    slimStat('total time', `${m.total_processing_seconds.toFixed(2)}s`),
    slimStat('AI invocations', fmtNum(m.ai_invocations)),
    slimStat('avg / record', m.avg_processing_ms_per_record ? `${m.avg_processing_ms_per_record.toFixed(2)}ms` : '—'),
  ].join('');

  const proof = document.getElementById('proofStrip');
  if (ev && state.baseline) {
    proof.style.display = 'flex';
    document.getElementById('proofUs').textContent = fmtPct(ev.false_match_rate, 1);
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

  // reason list (no chart -- ranked bars)
  const cc = Object.entries(m.category_counts || {}).sort((a, b) => b[1] - a[1]);
  const maxCount = cc.length ? cc[0][1] : 1;
  const reasonList = document.getElementById('reasonList');
  if (cc.length === 0) {
    reasonList.innerHTML = '<div class="empty-state">No exceptions in this run.</div>';
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
  const tbody = document.querySelector('#recentTable tbody');
  document.querySelector('#recentTable thead').innerHTML = '<tr><th>Payment</th><th>Amount</th><th>Decision</th></tr>';
  tbody.innerHTML = data.results.map((d) => `
    <tr data-payment="${d.payment_id}">
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
    `${fmtNum(m.total_payments)} records &nbsp;·&nbsp; <span style="color:var(--emerald)">${fmtNum((m.status_counts.AUTO_MATCH||0)+(m.status_counts.AI_ASSISTED_MATCH||0))} automatically reconciled</span> &nbsp;·&nbsp; <span style="color:var(--red)">${fmtNum(m.status_counts.EXCEPTION||0)} sent for review</span>`;

  const cols = ['payment_id', 'customer_name', 'amount', 'status', 'category', 'matched_bank_ref', 'confidence', 'method', 'ai_used'];
  document.querySelector('#decisionsTable thead').innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
  const tbody = document.querySelector('#decisionsTable tbody');
  if (data.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state">No decisions match this filter.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = data.results.map((d) => `
    <tr data-payment="${d.payment_id}">
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
    document.getElementById('drawerSubtitle').innerHTML = `${escapeHtml(payment?.customer_name || '')} — ${fmtAmount(payment?.amount)}`;

    let resolveButtonHtml = '';
    if (exception) {
      resolveButtonHtml = exception.resolved
        ? `<span class="badge resolved" style="margin-top:14px">Marked resolved</span>`
        : `<button class="btn btn-danger-outline" id="btnResolve" style="margin-top:14px" data-exc="${exception.id}">Resolve manually</button>`;
    }

    document.getElementById('drawerBody').innerHTML = `
      <div>${statusBadge(decision.status)} ${decision.category ? `<span class="badge exception" style="margin-left:6px">${catLabel(decision.category)}</span>` : ''}</div>
      <h4>Evidence checklist</h4>
      ${renderChecklist(candidate)}
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
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

async function refreshExceptions() {
  if (!state.runId) return;
  const category = document.getElementById('excFilterCategory').value;
  const data = await apiGet('/exceptions', { run_id: state.runId, category, limit: 300 });
  document.getElementById('excCount').textContent = data.results.length;
  const list = document.getElementById('exceptionsList');
  if (data.results.length === 0) {
    list.innerHTML = '<div class="empty-state">No exceptions to review.</div>';
    return;
  }
  list.innerHTML = data.results.map((r, i) => {
    const ev = r.evidence || {};
    const candidate = (ev.evidence_found && ev.evidence_found.candidates_considered && ev.evidence_found.candidates_considered[0]) || null;
    return `<div class="exc-card ${r.resolved ? 'is-resolved' : ''}" id="exc-${i}">
      <div class="exc-head" onclick="document.getElementById('exc-${i}').classList.toggle('open')">
        <span class="chev">&#9656;</span>
        <span class="exc-title"><span class="pid">${r.payment_id}</span> — ${escapeHtml(r.customer_name || '')} — ${fmtAmount(r.amount)}</span>
        ${r.resolved ? '<span class="badge resolved">Resolved</span>' : `<span class="badge exception"><span class="badge-dot"></span>${catLabel(r.category)}</span>`}
      </div>
      <div class="exc-body">
        <h4 style="margin-top:0">Evidence checklist</h4>
        ${renderChecklist(candidate)}
        <dl>
          <dt>Why unresolved</dt><dd>${escapeHtml(ev.why_unresolved || r.reason || '')}</dd>
          <dt>Suggested next action</dt><dd>${escapeHtml(r.suggested_action || '')}</dd>
        </dl>
        ${r.resolved ? '' : `<button class="btn btn-danger-outline" data-exc="${r.id}" onclick="resolveException(${r.id}, this)">Resolve manually</button>`}
      </div>
    </div>`;
  }).join('');
}
document.getElementById('excFilterCategory').addEventListener('change', refreshExceptions);

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

function renderEvaluation() {
  const ev = state.evaluation;
  if (!ev) return;
  const o = ev.outcomes;

  document.getElementById('evalHeroKpis').innerHTML = [
    heroKpi('Automation precision', fmtPct(ev.automation_precision), 'emerald'),
    heroKpi('Coverage (recall)', fmtPct(ev.coverage_recall)),
    heroKpi('Safety rate', fmtPct(ev.safety_rate), 'emerald'),
    heroKpi('False-match rate', fmtPct(ev.false_match_rate, 2), ev.false_match_rate > 0 ? 'red' : ''),
  ].join('');

  upsertChart('outcomes', 'chartOutcomes', {
    type: 'bar',
    data: {
      labels: Object.keys(o),
      datasets: [{ data: Object.values(o), backgroundColor: Object.keys(o).map((k) => OUTCOME_COLORS[k]), borderRadius: 4, maxBarThickness: 60 }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#1e1e23' } }, x: { grid: { display: false } } } },
  });

  if (state.baseline) {
    const b = state.baseline;
    const rows = [
      ['Precision', ev.automation_precision, b.automation_precision],
      ['Coverage', ev.coverage_recall, b.coverage_recall],
      ['Safety rate', ev.safety_rate, b.safety_rate],
      ['False-match rate', ev.false_match_rate, b.false_match_rate],
    ];
    document.querySelector('#baselineTable thead').innerHTML = '<tr><th>Metric</th><th>Veyra</th><th>Naive baseline</th></tr>';
    document.querySelector('#baselineTable tbody').innerHTML = rows.map(([label, us, naive]) => `
      <tr><td>${label}</td><td class="mono" style="color:var(--emerald);font-weight:600">${fmtPct(us, 1)}</td><td class="mono" style="color:var(--red)">${fmtPct(naive, 1)}</td></tr>
    `).join('');
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
      <td class="mono" style="color:var(--emerald)">${r.CORRECT_AUTO}</td>
      <td class="mono" style="color:var(--red)">${r.INCORRECT_AUTO}</td>
      <td class="mono" style="color:var(--amber)">${r.MISSED_OPPORTUNITY}</td>
      <td class="mono" style="color:#8b2f2f">${r.UNSAFE_AUTO}</td>
      <td class="mono" style="color:var(--blue)">${r.CORRECTLY_ESCALATED}</td>
    </tr>`).join('');
}

// ---------------------------------------------------------------------------
// Audit -- rendered as a terminal-style event log
// ---------------------------------------------------------------------------

function logLine(time, event, eventCls, detailHtml) {
  return `<div class="log-line">
    <div class="log-head"><span class="log-time">${time}</span><span class="log-event ${eventCls}">${event}</span></div>
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
    container.innerHTML = '<div class="empty-state">No audit entries.</div>';
    return;
  }

  const lines = [];
  const m = state.runDetail && state.runDetail.metrics;
  if (!paymentId && m) {
    lines.push(logLine(new Date(state.runDetail.started_at).toLocaleTimeString(), 'RECONCILIATION_STARTED', 'system',
      `batch=<b>${state.runId}</b> &nbsp; records=<b>${m.total_payments}</b>`));
  }
  entries.forEach((a) => {
    const time = new Date(a.created_at).toLocaleTimeString();
    if (a.status === 'EXCEPTION') {
      lines.push(logLine(time, 'MATCH_REJECTED', 'rejected', `<b>${a.payment_id}</b> &nbsp; reason=${catLabel(a.category)}`));
    } else {
      lines.push(logLine(time, 'MATCH_ACCEPTED', 'accepted', `<b>${a.payment_id}</b> &nbsp; confidence=${a.confidence ?? '—'} &nbsp; actor=${a.actor}`));
    }
  });
  if (!paymentId && m) {
    const auto = (m.status_counts.AUTO_MATCH || 0) + (m.status_counts.AI_ASSISTED_MATCH || 0);
    lines.push(logLine(new Date(state.runDetail.finished_at).toLocaleTimeString(), 'BATCH_COMPLETED', 'system',
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
// dataset generation / reconciliation actions
// ---------------------------------------------------------------------------

document.getElementById('btnGenerate').addEventListener('click', async () => {
  const btn = document.getElementById('btnGenerate');
  const status = document.getElementById('controlStatus');
  btn.disabled = true;
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
    btn.disabled = false;
  }
});

document.getElementById('btnRun').addEventListener('click', async () => {
  const btn = document.getElementById('btnRun');
  const status = document.getElementById('controlStatus');
  btn.disabled = true;
  status.textContent = 'Ingesting, matching, and reasoning over the batch…';
  try {
    const metrics = await apiPost('/reconcile/run');
    status.textContent = `Run ${metrics.run_id} complete: ${metrics.total_payments} records in ${metrics.total_processing_seconds}s`;
    toast('Reconciliation complete.', 'success');
    await loadRuns(metrics.run_id);
    await loadRunData();
  } catch (e) {
    toast(e.message, 'error');
    status.textContent = '';
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

(async function init() {
  try {
    await loadMetaAndHealth();
    await loadRuns();
    await loadRunData();
  } catch (e) {
    toast(e.message, 'error');
  }
})();
