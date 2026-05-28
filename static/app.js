/* ═══════════════════════════════════════════════════════════════
   ChurnSight  –  Frontend Application Logic
═══════════════════════════════════════════════════════════════ */

const API = `${window.location.origin}/api`;

/* ── State ──────────────────────────────────────────────────── */
let state = {
  modelTrained   : false,
  algorithm      : 'random_forest',
  selectedFile   : null,
  results        : null,
  allCustomers   : [],
  filteredCustomers: [],
  currentPage    : 1,
  pageSize       : 20,
  riskFilter     : 'All',
  searchQuery    : '',
  charts         : {}
};

/* ── DOM helpers ─────────────────────────────────────────────── */
const $  = id => document.getElementById(id);
const qs = s  => document.querySelector(s);

/* ══════════════════  NAVIGATION  ═════════════════════════════ */
const sections = ['upload','overview','analytics','customers','predict'];
const titles   = {
  upload   : 'Upload & Train Model',
  overview : 'Overview Dashboard',
  analytics: 'Analytics & Model Performance',
  customers: 'Customer Risk List',
  predict  : 'Predict Single Customer'
};

function navigateTo(section) {
  sections.forEach(s => {
    $(`section-${s}`).classList.toggle('active', s === section);
    $(`nav-${s}`).classList.toggle('active', s === section);
  });
  $('page-title').textContent = titles[section];

  // Close sidebar on mobile
  if (window.innerWidth <= 768) {
    document.querySelector('.sidebar').classList.remove('open');
  }
}

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const sec = el.dataset.section;
    if (!state.modelTrained && sec !== 'upload' && sec !== 'predict') {
      showToast('Please upload data and train a model first.', 'error');
      return;
    }
    navigateTo(sec);
  });
});

$('menu-toggle').addEventListener('click', () => {
  document.querySelector('.sidebar').classList.toggle('open');
});

/* ══════════════════  CLOCK  ═══════════════════════════════════ */
function updateClock() {
  const now = new Date();
  $('time-display').textContent = now.toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}
updateClock();
setInterval(updateClock, 1000);

/* ══════════════════  FILE UPLOAD  ══════════════════════════════ */
const dropZone  = $('drop-zone');
const fileInput = $('file-input');

dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('dragging');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragging'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragging');
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) setFile(fileInput.files[0]);
});

function setFile(file) {
  if (!file.name.endsWith('.csv')) {
    showToast('Please upload a CSV file.', 'error');
    return;
  }
  state.selectedFile = file;
  $('file-name-display').textContent = file.name;
  $('file-size-display').textContent = formatBytes(file.size);
  $('file-info').style.display = 'flex';
  $('drop-zone').style.display = 'none';
  $('train-btn').disabled = false;
}

$('remove-file').addEventListener('click', () => {
  state.selectedFile = null;
  fileInput.value = '';
  $('file-info').style.display = 'none';
  $('drop-zone').style.display = 'block';
  $('train-btn').disabled = true;
});

/* ── Algorithm pills ─────────────────────────────────────────── */
document.querySelectorAll('.algo-pill').forEach(pill => {
  pill.addEventListener('click', () => {
    document.querySelectorAll('.algo-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    state.algorithm = pill.dataset.algo;
  });
});

/* ── Train button ────────────────────────────────────────────── */
$('train-btn').addEventListener('click', trainModel);

async function trainModel() {
  if (!state.selectedFile) return;

  showLoading('Preprocessing data & training model…');
  showProgress(10, 'Uploading data…');

  const fd = new FormData();
  fd.append('file', state.selectedFile);
  fd.append('algorithm', state.algorithm);

  try {
    showProgress(30, 'Training model…');

    const res  = await fetch(`${API}/upload`, { method: 'POST', body: fd });
    const data = await res.json();

    if (!res.ok) {
      hideLoading();
      showToast(data.error || 'Training failed.', 'error');
      return;
    }

    showProgress(80, 'Building visualizations…');
    await sleep(400);
    showProgress(100, 'Done!');
    await sleep(500);

    state.results   = data;
    state.modelTrained = true;
    state.allCustomers = data.customers || [];

    hideLoading();
    updateModelStatus(data.summary.algorithm);
    populateDashboard(data);
    showToast(`Model trained! Accuracy: ${data.metrics.accuracy}%`, 'success');
    navigateTo('overview');

  } catch (err) {
    hideLoading();
    showToast(`Connection error: ${err.message}`, 'error');
  }
}

/* ══════════════════  DASHBOARD POPULATION  ════════════════════ */
function populateDashboard(data) {
  const { metrics, summary, confusion_matrix: cm,
          roc_curve: roc, prob_distribution: pd,
          feature_importance: fi, trend_data: td } = data;

  /* KPI Cards */
  animateValue('kpi-total-val', 0, summary.total_customers, 800);
  $('kpi-churn-val').textContent = summary.churn_rate + '%';
  animateValue('kpi-high-val', 0, summary.high_risk, 800);
  $('kpi-acc-val').textContent  = metrics.accuracy + '%';
  $('kpi-f1-val').textContent   = metrics.f1_score + '%';
  $('kpi-auc-val').textContent  = metrics.auc_roc + '%';

  /* Algorithm badge */
  const badge = $('algo-badge');
  badge.textContent = summary.algorithm;
  badge.style.display = 'flex';

  /* Charts */
  buildTrendChart(td);
  buildRiskDonut(summary);
  buildProbDistChart(pd);
  buildROCChart(roc);
  buildFeatureChart(fi);
  buildRingCharts(metrics);

  /* Confusion matrix */
  $('cm-tn').textContent = cm[0][0];
  $('cm-fp').textContent = cm[0][1];
  $('cm-fn').textContent = cm[1][0];
  $('cm-tp').textContent = cm[1][1];

  /* Ring values */
  $('ring-acc-val').textContent  = metrics.accuracy  + '%';
  $('ring-prec-val').textContent = metrics.precision + '%';
  $('ring-rec-val').textContent  = metrics.recall    + '%';
  $('ring-f1-val').textContent   = metrics.f1_score  + '%';

  /* Customer table */
  state.filteredCustomers = [...state.allCustomers];
  renderCustomerTable();
}

/* ══════════════════  CHART BUILDERS  ══════════════════════════ */
const chartDefaults = {
  color: '#94a3b8',
  font : { family: 'Inter' }
};
Chart.defaults.color = chartDefaults.color;
Chart.defaults.font.family = chartDefaults.font.family;

function destroyChart(key) {
  if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
}

/* Trend Chart */
function buildTrendChart(td) {
  destroyChart('trend');
  const ctx = $('trendChart').getContext('2d');
  state.charts.trend = new Chart(ctx, {
    type: 'line',
    data: {
      labels: td.months,
      datasets: [
        {
          label: 'Churn Rate %',
          data: td.churn_rate,
          borderColor: '#f87171',
          backgroundColor: 'rgba(248,113,113,0.08)',
          tension: 0.4,
          fill: true,
          pointBackgroundColor: '#f87171',
          pointRadius: 4,
          pointHoverRadius: 6,
          borderWidth: 2.5
        },
        {
          label: 'Retention %',
          data: td.retained,
          borderColor: '#34d399',
          backgroundColor: 'rgba(52,211,153,0.06)',
          tension: 0.4,
          fill: true,
          pointBackgroundColor: '#34d399',
          pointRadius: 4,
          pointHoverRadius: 6,
          borderWidth: 2.5
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, pointStyleWidth: 10, padding: 18 } },
        tooltip: tooltipStyle()
      },
      scales: {
        x: gridStyle(),
        y: { ...gridStyle(), min: 0, max: 100, ticks: { callback: v => v + '%' } }
      }
    }
  });
}

/* Risk Donut */
function buildRiskDonut(summary) {
  destroyChart('donut');
  const ctx = $('riskDonutChart').getContext('2d');
  const labels = ['High Risk', 'Medium Risk', 'Low Risk'];
  const data   = [summary.high_risk, summary.medium_risk, summary.low_risk];
  const colors = ['#f87171', '#fb923c', '#34d399'];

  state.charts.donut = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderColor: 'transparent', hoverOffset: 8, borderWidth: 0 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: '72%',
      plugins: {
        legend: { display: false },
        tooltip: tooltipStyle()
      }
    }
  });

  /* Custom legend */
  const legend = $('donut-legend');
  legend.innerHTML = labels.map((l, i) => `
    <div class="legend-item">
      <div class="legend-dot" style="background:${colors[i]}"></div>
      <span>${l}: <b>${data[i]}</b></span>
    </div>`).join('');
}

/* Prob Distribution */
function buildProbDistChart(pd) {
  destroyChart('probDist');
  const ctx = $('probDistChart').getContext('2d');
  const bgColors = pd.labels.map((_, i) => {
    const pct = i / (pd.labels.length - 1);
    return `rgba(${Math.round(99 + pct * 155)},${Math.round(102 - pct * 80)},${Math.round(241 - pct * 170)},0.75)`;
  });

  state.charts.probDist = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: pd.labels.map(l => l + '%'),
      datasets: [{
        label: 'Customers',
        data: pd.counts,
        backgroundColor: bgColors,
        borderRadius: 6,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: { display: true, text: 'Churn Probability Range', color: '#64748b' } },
        y: { ...gridStyle(), title: { display: true, text: 'Number of Customers', color: '#64748b' } }
      }
    }
  });
}

/* ROC Curve */
function buildROCChart(roc) {
  destroyChart('roc');
  const ctx = $('rocChart').getContext('2d');
  state.charts.roc = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'ROC Curve',
          data: roc.fpr.map((x, i) => ({ x, y: roc.tpr[i] })),
          borderColor: '#818cf8',
          backgroundColor: 'rgba(129,140,248,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2.5
        },
        {
          label: 'Random Classifier',
          data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
          borderColor: 'rgba(255,255,255,0.12)',
          borderDash: [6, 4],
          pointRadius: 0,
          borderWidth: 1.5,
          fill: false
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, padding: 14 } },
        tooltip: tooltipStyle()
      },
      scales: {
        x: { ...gridStyle(), type: 'linear', min: 0, max: 1, title: { display: true, text: 'False Positive Rate', color: '#64748b' } },
        y: { ...gridStyle(), min: 0, max: 1, title: { display: true, text: 'True Positive Rate', color: '#64748b' } }
      }
    }
  });
}

/* Feature Importance */
function buildFeatureChart(fi) {
  destroyChart('feature');
  const ctx = $('featureChart').getContext('2d');
  const top = fi.slice(0, 10);

  state.charts.feature = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(f => f.feature.replace(/_/g, ' ')),
      datasets: [{
        label: 'Importance',
        data: top.map(f => f.importance),
        backgroundColor: top.map((_, i) => `hsla(${240 + i * 12}, 70%, 65%, 0.8)`),
        borderRadius: 6,
        borderSkipped: false
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: { display: true, text: 'Importance Score', color: '#64748b' } },
        y: gridStyle()
      }
    }
  });
}

/* Ring (Doughnut) Charts */
function buildRingCharts(metrics) {
  const rings = [
    { canvas: 'ringAccCanvas',  value: metrics.accuracy,  color: '#34d399' },
    { canvas: 'ringPrecCanvas', value: metrics.precision, color: '#60a5fa' },
    { canvas: 'ringRecCanvas',  value: metrics.recall,    color: '#f59e0b' },
    { canvas: 'ringF1Canvas',   value: metrics.f1_score,  color: '#a78bfa' }
  ];

  rings.forEach(({ canvas, value, color }) => {
    destroyChart(canvas);
    const ctx = $(canvas).getContext('2d');
    state.charts[canvas] = new Chart(ctx, {
      type: 'doughnut',
      data: {
        datasets: [{
          data: [value, 100 - value],
          backgroundColor: [color, 'rgba(255,255,255,0.05)'],
          borderWidth: 0,
          hoverOffset: 0
        }]
      },
      options: {
        responsive: false,
        cutout: '78%',
        animation: { animateRotate: true, duration: 900 },
        plugins: { legend: { display: false }, tooltip: { enabled: false } }
      }
    });
  });
}

/* ══════════════════  GAUGE CHART  ════════════════════════════ */
function drawGauge(canvas, probability) {
  const ctx    = $(canvas).getContext('2d');
  const W = 220, H = 130;
  const cx = W / 2, cy = H - 18;
  const r = 90;

  ctx.clearRect(0, 0, W, H);

  // Background arc
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0, false);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth   = 16;
  ctx.lineCap     = 'round';
  ctx.stroke();

  // Value arc
  const pct   = probability / 100;
  const end   = Math.PI + pct * Math.PI;
  const grad  = ctx.createLinearGradient(cx - r, cy, cx + r, cy);
  if (probability >= 70) {
    grad.addColorStop(0, '#fbbf24');
    grad.addColorStop(1, '#f87171');
  } else if (probability >= 40) {
    grad.addColorStop(0, '#34d399');
    grad.addColorStop(1, '#fbbf24');
  } else {
    grad.addColorStop(0, '#60a5fa');
    grad.addColorStop(1, '#34d399');
  }

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, end, false);
  ctx.strokeStyle = grad;
  ctx.lineWidth   = 16;
  ctx.lineCap     = 'round';
  ctx.stroke();

  // Needle
  const needleAngle = Math.PI + pct * Math.PI;
  const nx = cx + (r - 26) * Math.cos(needleAngle);
  const ny = cy + (r - 26) * Math.sin(needleAngle);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(nx, ny);
  ctx.strokeStyle = '#fff';
  ctx.lineWidth   = 2.5;
  ctx.lineCap     = 'round';
  ctx.stroke();

  // Center dot
  ctx.beginPath();
  ctx.arc(cx, cy, 5, 0, Math.PI * 2);
  ctx.fillStyle = '#fff';
  ctx.fill();

  // Tick labels
  ctx.font = '500 10px Inter';
  ctx.fillStyle = 'rgba(255,255,255,0.4)';
  ctx.textAlign = 'center';
  ctx.fillText('0%',  cx - r + 4, cy + 18);
  ctx.fillText('50%', cx,          cy - r - 8);
  ctx.fillText('100%',cx + r - 4, cy + 18);
}

/* ══════════════════  CUSTOMER TABLE  ════════════════════════ */
function renderCustomerTable() {
  let data = [...state.allCustomers];

  // Filter by risk
  if (state.riskFilter !== 'All') {
    data = data.filter(c => c.risk_level === state.riskFilter);
  }
  // Filter by search
  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    data = data.filter(c => c.id.toLowerCase().includes(q));
  }

  state.filteredCustomers = data;
  const total  = data.length;
  const pages  = Math.ceil(total / state.pageSize);
  const start  = (state.currentPage - 1) * state.pageSize;
  const paged  = data.slice(start, start + state.pageSize);

  $('customer-count').textContent = `Showing ${paged.length} of ${total} customers`;

  const tbody = $('customer-tbody');
  if (paged.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No customers match the current filters.</td></tr>';
  } else {
    tbody.innerHTML = paged.map(c => {
      const fillClass = c.risk_level === 'High' ? 'fill-high' : c.risk_level === 'Medium' ? 'fill-medium' : 'fill-low';
      return `
        <tr>
          <td><strong>${c.id}</strong></td>
          <td><span class="risk-tag ${c.risk_level}">${c.risk_level}</span></td>
          <td>
            <div class="prob-bar-wrap">
              <div class="prob-mini-bar">
                <div class="prob-mini-fill ${fillClass}" style="width:${c.churn_prob}%"></div>
              </div>
              <span>${c.churn_prob}%</span>
            </div>
          </td>
          <td>${c.tenure} mo</td>
          <td>$${c.monthly.toFixed(2)}</td>
          <td>${c.support}</td>
          <td>${c.last_login} days</td>
          <td>${c.actual === 1
            ? '<span class="churn-yes">● Churned</span>'
            : '<span class="churn-no">● Retained</span>'
          }</td>
        </tr>`;
    }).join('');
  }

  // Pagination
  renderPagination(pages);
}

function renderPagination(pages) {
  const wrap = $('table-pagination');
  if (pages <= 1) { wrap.innerHTML = ''; return; }

  let html = `<button class="page-btn" ${state.currentPage === 1 ? 'disabled' : ''} data-page="${state.currentPage - 1}">‹ Prev</button>`;

  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 3 && i < pages - 1 && Math.abs(i - state.currentPage) > 1) {
      if (i === 4) html += '<span style="color:var(--text-3);padding:0 4px">…</span>';
      continue;
    }
    html += `<button class="page-btn ${i === state.currentPage ? 'active' : ''}" data-page="${i}">${i}</button>`;
  }

  html += `<button class="page-btn" ${state.currentPage === pages ? 'disabled' : ''} data-page="${state.currentPage + 1}">Next ›</button>`;
  wrap.innerHTML = html;

  wrap.querySelectorAll('.page-btn[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.currentPage = parseInt(btn.dataset.page);
      renderCustomerTable();
    });
  });
}

/* Risk filter buttons */
document.querySelectorAll('.risk-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.risk-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.riskFilter   = btn.dataset.risk;
    state.currentPage  = 1;
    renderCustomerTable();
  });
});

/* Search */
$('customer-search').addEventListener('input', e => {
  state.searchQuery = e.target.value.trim();
  state.currentPage = 1;
  renderCustomerTable();
});

/* ══════════════════  SINGLE PREDICTION  ════════════════════ */
$('predict-form').addEventListener('submit', async e => {
  e.preventDefault();

  if (!state.modelTrained) {
    showToast('Please train a model first.', 'error');
    return;
  }

  const btn = $('predict-btn');
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner-inline"></span> Predicting…`;

  const payload = {
    age            : +$('p-age').value,
    gender         : $('p-gender').value,
    tenure_months  : +$('p-tenure').value,
    monthly_charges: +$('p-monthly').value,
    total_charges  : +$('p-total').value,
    num_products   : +$('p-products').value,
    num_logins_last30: +$('p-logins').value,
    support_tickets: +$('p-tickets').value,
    last_login_days: +$('p-lastlogin').value,
    location       : $('p-location').value,
    contract_type  : $('p-contract').value,
    payment_method : $('p-payment').value
  };

  try {
    const res  = await fetch(`${API}/predict`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (!res.ok) {
      showToast(data.error || 'Prediction failed.', 'error');
      return;
    }

    showPredictionResult(data);
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg> Predict Churn`;
  }
});

function showPredictionResult(data) {
  $('result-placeholder').style.display = 'none';
  $('result-content').style.display     = 'block';

  const prob  = data.churn_probability;
  const risk  = data.risk_level;
  const recs  = data.recommendation || [];

  // Gauge
  drawGauge('gaugeCanvas', prob);
  $('gauge-val').textContent = prob + '%';

  // Risk badge
  const badge = $('risk-badge-large');
  badge.className = `risk-badge-large ${risk}`;
  $('risk-label-large').textContent = risk + ' Risk';

  // Recommendations
  $('recs-list').innerHTML = recs.map(r => `<li>${r}</li>`).join('');

  // Animate gauge value
  animateCounter('gauge-val', 0, prob, 900, v => v.toFixed(1) + '%');
}

/* ══════════════════  HELPERS  ══════════════════════════════ */
function tooltipStyle() {
  return {
    backgroundColor: 'rgba(15,19,34,0.95)',
    borderColor    : 'rgba(255,255,255,0.1)',
    borderWidth    : 1,
    titleFont      : { weight: '700' },
    bodyFont       : { size: 12 },
    padding        : 10,
    cornerRadius   : 8
  };
}

function gridStyle() {
  return {
    grid  : { color: 'rgba(255,255,255,0.05)' },
    ticks : { color: '#64748b', font: { size: 11 } },
    border: { color: 'transparent' }
  };
}

function showLoading(text = 'Processing…') {
  $('loading-text').textContent = text;
  $('loading-overlay').style.display = 'flex';
}
function hideLoading() {
  $('loading-overlay').style.display = 'none';
  $('progress-wrap').style.display = 'none';
}

function showProgress(pct, label) {
  $('progress-wrap').style.display = 'block';
  $('progress-fill').style.width   = pct + '%';
  $('progress-label').textContent  = label;
}

function showToast(msg, type = 'success') {
  const t = $('toast');
  t.textContent = msg;
  t.className   = `toast ${type} show`;
  setTimeout(() => t.classList.remove('show'), 3500);
}

function updateModelStatus(algo) {
  const badge = $('model-status-badge');
  badge.innerHTML = `<span class="status-dot active"></span><span>${algo} Ready</span>`;
}

function formatBytes(bytes) {
  if (bytes < 1024)       return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function animateValue(id, from, to, duration) {
  const el    = $(id);
  const start = performance.now();
  const range = to - from;
  function step(now) {
    const t = Math.min((now - start) / duration, 1);
    const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    el.textContent = Math.round(from + range * e);
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

function animateCounter(id, from, to, duration, fmt = v => Math.round(v)) {
  const el    = $(id);
  const start = performance.now();
  const range = to - from;
  function step(now) {
    const t = Math.min((now - start) / duration, 1);
    const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    el.textContent = fmt(from + range * e);
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ══════════════════  SAMPLE DATA SERVE  ════════════════════ */
// Wire up download sample button to Flask static route
$('download-sample').href = `${location.origin}/sample_data.csv`;

/* ══════════════════  INIT  ════════════════════════════════ */
navigateTo('upload');
