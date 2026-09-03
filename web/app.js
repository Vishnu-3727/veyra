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

const state = {
  meta: null, health: null, runs: [], runId: null, runDetail: null,
  evaluation: null, baseline: null, decisions: null, charts: {},
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

function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3200);
}

async function apiGet(path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''));
  const url = `${API_BASE}${path}${qs.toString() ? '?' + qs.toString() : ''}`;
  setLoading(true);
  try {
    const res = await fetch(url);
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

async function apiPost(path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''));
  const url = `${API_BASE}${path}${qs.toString() ? '?' + qs.toString() : ''}`;
  setLoading(true);
  try {
    const res = await fetch(url, { method: 'POST' });
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

// recursive renderer for evidence JSON -> a designed key/value tree instead of a raw dump
function renderKV(value, depth = 0) {
  if (value === null || value === undefined) return '<span class="v-null">null</span>';
  if (Array.isArray(value)) {
    if (value.length === 0) return '<span class="v-null">[]</span>';
    if (typeof value[0] !== 'object') {
      return value.map((v) => renderKV(v)).join(', ');
    }
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
// Every chart lives in a `.chart-box` with an explicit CSS height (see
// styles.css). Without this, Chart.js ignores that height and sizes the
// canvas from its own aspect ratio based on container width -- which is
// what was blowing the donut/bar charts up to hundreds of pixels tall on
// wide viewports and overlapping the footer.
Chart.defaults.maintainAspectRatio = false;
Chart.defaults.responsive = true;

function upsertChart(key, canvasId, config) {
  if (state.charts[key]) state.charts[key].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  state.charts[key] = new Chart(ctx, config);
}

// ---------------------------------------------------------------------------
// tabs
// ---------------------------------------------------------------------------

function switchView(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
}

document.getElementById('tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (tab) switchView(tab.dataset.view);
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
  if (!health) {
    pill.className = 'status-pill off';
    text.textContent = 'API unreachable';
  } else if (health.ai_enabled) {
    pill.className = 'status-pill on';
    text.textContent = `AI enabled (${health.llm_model})`;
  } else {
    pill.className = 'status-pill off';
    text.textContent = 'AI disabled — fallback mode';
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
    await Promise.all([refreshDecisions(), refreshExceptions(), refreshAudit()]);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

function kpiCard(label, value, opts = {}) {
  const { sub = '', cls = '', help = '' } = opts;
  return `<div class="kpi">
    <div class="kpi-label">${label}${help ? `<span class="kpi-help" title="${escapeHtml(help)}">?</span>` : ''}</div>
    <div class="kpi-value ${cls}">${value}</div>
    ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}
  </div>`;
}

function renderOverview() {
  const m = state.runDetail.metrics;
  const ev = state.evaluation;
  const sc = m.status_counts;
  const total = m.total_payments;
  const auto = sc.AUTO_MATCH || 0, ai = sc.AI_ASSISTED_MATCH || 0, exc = sc.EXCEPTION || 0;

  document.getElementById('overviewCaption').innerHTML =
    `"We automate the financial decisions we can support with evidence, explain those decisions, and safely surface the ones we cannot."
     &nbsp;—&nbsp; run <span class="mono">${state.runId}</span> · started ${new Date(state.runDetail.started_at).toLocaleString()}`;

  document.getElementById('kpiPrimary').innerHTML = [
    kpiCard('Total processed', fmtNum(total)),
    kpiCard('Auto-reconciled', fmtNum(auto), { cls: 'emerald', help: 'Deterministic rule match -- no AI involved.' }),
    kpiCard('AI-assisted matches', fmtNum(ai), { cls: 'brass', help: 'AI proposed a match AND it passed every policy guardrail.' }),
    kpiCard('Exceptions', fmtNum(exc), { cls: exc > 0 ? 'red' : '', help: 'Unresolved -- routed to human review with structured evidence.' }),
    kpiCard('Reconciliation rate', total ? fmtPct((auto + ai) / total) : '—'),
  ].join('');

  document.getElementById('kpiSecondary').innerHTML = [
    kpiCard('Throughput', m.throughput_per_second ? `${m.throughput_per_second.toFixed(1)}/s` : '—'),
    kpiCard('Total processing time', `${m.total_processing_seconds.toFixed(2)}s`),
    kpiCard('AI invocations', fmtNum(m.ai_invocations), { help: 'Cases genuinely escalated for semantic reasoning.' }),
    kpiCard('Avg time / record', m.avg_processing_ms_per_record ? `${m.avg_processing_ms_per_record.toFixed(2)}ms` : '—'),
  ].join('');

  const proof = document.getElementById('proofStrip');
  if (ev && state.baseline) {
    proof.style.display = 'flex';
    document.getElementById('proofUs').textContent = fmtPct(ev.false_match_rate, 1);
    document.getElementById('proofBaseline').textContent = fmtPct(state.baseline.false_match_rate, 1);
  } else {
    proof.style.display = 'none';
  }

  upsertChart('mix', 'chartMix', {
    type: 'doughnut',
    data: {
      labels: Object.keys(sc).map((k) => (state.meta.statuses[k] || k)),
      datasets: [{ data: Object.values(sc), backgroundColor: Object.keys(sc).map((k) => STATUS_COLORS[k]), borderWidth: 0 }],
    },
    options: { plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 14 } } }, cutout: '62%' },
  });

  const cc = m.category_counts || {};
  const ccEntries = Object.entries(cc).sort((a, b) => b[1] - a[1]);
  upsertChart('categories', 'chartCategories', {
    type: 'bar',
    data: {
      labels: ccEntries.map(([k]) => catLabel(k)),
      datasets: [{ data: ccEntries.map(([, v]) => v), backgroundColor: '#6ea8ea', borderRadius: 4, maxBarThickness: 22 }],
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: { x: { grid: { color: '#1e1e23' } }, y: { grid: { display: false } } },
    },
  });

  if (ccEntries.length === 0) {
    document.querySelector('#chartCategories').closest('.panel').querySelector('.chart-box').innerHTML =
      '<div class="empty-state">No exceptions in this run.</div>';
  }
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

async function refreshDecisions() {
  if (!state.runId) return;
  const status = document.getElementById('filterStatus').value;
  const category = document.getElementById('filterCategory').value;
  const data = await apiGet('/decisions', { run_id: state.runId, status, category, limit: 300 });
  state.decisions = data;
  document.getElementById('decisionsCount').textContent = `${data.results.length} of ${data.total} shown`;

  const cols = ['payment_id', 'customer_name', 'amount', 'status', 'category', 'matched_bank_ref', 'confidence', 'method', 'ai_used'];
  const thead = document.querySelector('#decisionsTable thead');
  thead.innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
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
    const { payment, decision, audit_trail } = detail;
    document.getElementById('drawerTitle').textContent = paymentId;
    document.getElementById('drawerSubtitle').innerHTML = `${escapeHtml(payment?.customer_name || '')} — ${fmtAmount(payment?.amount)}`;
    document.getElementById('drawerBody').innerHTML = `
      <div>${statusBadge(decision.status)} ${decision.category ? `<span class="badge exception" style="margin-left:6px">${catLabel(decision.category)}</span>` : ''}</div>
      <h4>Reason</h4>
      <div style="font-size:13.5px">${escapeHtml(decision.reason || '')}</div>
      <h4>Decision facts</h4>
      <div class="kv-tree">${renderKV({
        confidence: decision.confidence, method: decision.method, ai_used: !!decision.ai_used,
        matched_bank_ref: decision.matched_bank_ref, invoice_id: decision.invoice_id, invoice_status: decision.invoice_status,
        processing_ms: decision.processing_ms, created_at: decision.created_at,
      })}</div>
      <h4>Evidence</h4>
      <div class="kv-tree">${renderKV(decision.evidence || {})}</div>
      <h4>Audit trail (${audit_trail.length})</h4>
      <div class="kv-tree">${audit_trail.map((a) => renderKV({ actor: a.actor, status: a.status, category: a.category, confidence: a.confidence, created_at: a.created_at })).join('<hr style="border-color:var(--border-soft);margin:8px 0">')}</div>
    `;
    openDrawer();
  } catch (e) {
    toast(e.message, true);
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
    return `<div class="exc-card" id="exc-${i}">
      <div class="exc-head" onclick="document.getElementById('exc-${i}').classList.toggle('open')">
        <span class="chev">&#9656;</span>
        <span class="exc-title"><span class="pid">${r.payment_id}</span> — ${escapeHtml(r.customer_name || '')} — ${fmtAmount(r.amount)}</span>
        <span class="badge exception"><span class="badge-dot"></span>${catLabel(r.category)}</span>
      </div>
      <div class="exc-body">
        <dl>
          <dt>Why unresolved</dt><dd>${escapeHtml(ev.why_unresolved || r.reason || '')}</dd>
          <dt>Suggested next action</dt><dd>${escapeHtml(r.suggested_action || '')}</dd>
          ${ev.attempted ? `<dt>Attempted</dt><dd>${ev.attempted.map(escapeHtml).join(', ')}</dd>` : ''}
          ${ev.evidence_found ? `<dt>Evidence found</dt><dd><div class="kv-tree">${renderKV(ev.evidence_found)}</div></dd>` : ''}
          ${ev.ai_evidence ? `<dt>AI reasoning</dt><dd><div class="kv-tree">${renderKV(ev.ai_evidence)}</div></dd>` : ''}
        </dl>
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
    const metrics = ['automation_precision', 'coverage_recall', 'safety_rate', 'false_match_rate'];
    const labels = ['Precision', 'Coverage', 'Safety rate', 'False-match rate'];
    upsertChart('baseline', 'chartBaseline', {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'Veyra', data: metrics.map((m) => (ev[m] || 0) * 100), backgroundColor: '#3ecf8e', borderRadius: 4, maxBarThickness: 34 },
          { label: 'Naive baseline', data: metrics.map((m) => (b[m] || 0) * 100), backgroundColor: '#ec6b6b', borderRadius: 4, maxBarThickness: 34 },
        ],
      },
      options: {
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 14 } } },
        scales: { y: { grid: { color: '#1e1e23' }, ticks: { callback: (v) => v + '%' }, max: 100 }, x: { grid: { display: false } } },
      },
    });
    document.getElementById('baselineFalseRate').textContent = fmtPct(b.false_match_rate);
    document.getElementById('usFalseRate').textContent = fmtPct(ev.false_match_rate);
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
// Audit
// ---------------------------------------------------------------------------

async function refreshAudit() {
  if (!state.runId) return;
  const paymentId = document.getElementById('auditPaymentFilter').value.trim();
  const data = await apiGet('/audit', { run_id: state.runId, payment_id: paymentId || undefined, limit: 300 });
  const cols = ['created_at', 'payment_id', 'actor', 'status', 'category', 'ai_used', 'confidence', 'reason'];
  document.querySelector('#auditTable thead').innerHTML = `<tr>${cols.map((c) => `<th>${c.replace(/_/g, ' ')}</th>`).join('')}</tr>`;
  const tbody = document.querySelector('#auditTable tbody');
  if (data.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state">No audit entries.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = data.results.map((a) => `
    <tr>
      <td class="mono faint">${new Date(a.created_at).toLocaleTimeString()}</td>
      <td class="mono">${a.payment_id}</td>
      <td class="muted">${a.actor}</td>
      <td>${statusBadge(a.status)}</td>
      <td class="muted">${catLabel(a.category)}</td>
      <td>${a.ai_used ? 'yes' : 'no'}</td>
      <td class="mono">${a.confidence ?? '—'}</td>
      <td class="muted" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(a.reason || '')}</td>
    </tr>`).join('');
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
    toast(e.message, true);
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
    toast('Reconciliation complete.');
    await loadRuns(metrics.run_id);
    await loadRunData();
  } catch (e) {
    toast(e.message, true);
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
    toast(e.message, true);
  }
})();
