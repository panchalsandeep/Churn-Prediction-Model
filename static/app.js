/* ═══════════════════════════════════════════════════════════════
   ChurnSight  –  Frontend Application Logic
═══════════════════════════════════════════════════════════════ */

const API = `${window.location.origin}/api`;

// ── Firebase Configuration ──────────────────────────────────────────────────
// REPLACE this placeholder config with your actual Web App credentials from the Firebase Console!
const firebaseConfig = {
  apiKey: "YOUR_API_KEY",
  authDomain: "YOUR_PROJECT_ID.firebaseapp.com",
  projectId: "YOUR_PROJECT_ID",
  storageBucket: "YOUR_PROJECT_ID.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId: "YOUR_APP_ID"
};

// Initialize Firebase Web SDK Compat
let auth = null;
if (firebaseConfig.apiKey && firebaseConfig.apiKey !== "YOUR_API_KEY") {
  try {
    firebase.initializeApp(firebaseConfig);
    auth = firebase.auth();
  } catch (err) {
    console.error("Firebase Init Error:", err);
  }
}

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
  charts         : {},
  user           : null,
  idToken        : null,
  admin          : {
    isAdmin: false,
    users: [],
    settings: null,
    modelStatus: null
  }
};

/* ── DOM helpers ─────────────────────────────────────────────────────── */
const $  = id => document.getElementById(id);
const qs = s  => document.querySelector(s);

/* ── Health Scores State ─────────────────────────────────────── */
const hsState = {
  trend    : 'all',
  risk     : 'all',
  sort     : 'score_asc',
  page     : 1,
  scores   : [],
  filtered : []
};

/* ══════════════════  NAVIGATION  ═════════════════════════════ */
const sections = ['upload','overview','analytics','customers','health','predict','admin'];
const titles   = {
  upload   : 'Upload & Train Model',
  overview : 'Overview Dashboard',
  analytics: 'Analytics & Model Performance',
  customers: 'Customer Risk List',
  health   : 'Customer Health Scores',
  predict  : 'Predict Single Customer',
  admin    : 'Admin Console'
};

function navigateTo(section) {
  sections.forEach(s => {
    $(`section-${s}`).classList.toggle('active', s === section);
    $(`nav-${s}`).classList.toggle('active', s === section);
  });
  $('page-title').textContent = titles[section];

  if (section === 'admin') {
    loadAdminPanel();
  }

  if (section === 'health') {
    if (state.modelTrained && !hsState.scores.length) buildHealthScores();
    applyHsFilters();
    updateHsKpis();
    renderHsGrid();
  }

  // Close sidebar on mobile
  if (window.innerWidth <= 768) {
    document.querySelector('.sidebar').classList.remove('open');
  }
}

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const sec = el.dataset.section;
    if (sec === 'admin' && !state.admin.isAdmin) {
      showToast('Admin access is restricted to administrators only.', 'error');
      return;
    }
    if (!state.modelTrained && sec !== 'upload' && sec !== 'predict' && sec !== 'admin' && sec !== 'health') {
      showToast('Please upload data and train a model first.', 'error');
      return;
    }
    navigateTo(sec);
  });
});

function updateAdminNavigation() {
  const adminNav = $('nav-admin');
  if (!adminNav) return;
  adminNav.style.display = state.admin.isAdmin ? 'flex' : 'none';
}

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

    const headers = {};
    if (state.idToken) {
      headers['Authorization'] = `Bearer ${state.idToken}`;
    }

    const res  = await fetch(`${API}/upload`, { 
      method: 'POST', 
      headers: headers, 
      body: fd 
    });
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

  /* Health Scores – build whenever new data arrives */
  buildHealthScores();
  applyHsFilters();
  updateHsKpis();
  if ($('section-health')?.classList.contains('active')) renderHsGrid();
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
    const headers = { 'Content-Type': 'application/json' };
    if (state.idToken) {
      headers['Authorization'] = `Bearer ${state.idToken}`;
    }

    const res  = await fetch(`${API}/predict`, {
      method: 'POST',
      headers: headers,
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

/* ══════════════════  FIREBASE AUTHENTICATION FLOW  ════════════════ */
let activeAuthTab = 'login'; // 'login' or 'register'

function initAuthUI() {
  if (!auth) {
    // Graceful fallback for local development without Firebase config
    console.log("[INFO] Bypassing Authentication Overlay (Operating in Dev Mode)");
    $('auth-overlay').style.display = 'none';
    return;
  }

  // Show Auth overlay initially while verifying auth state
  $('auth-overlay').style.display = 'flex';

  // Toggle Login/Register Tabs
  $('tab-login').addEventListener('click', () => setAuthTab('login'));
  $('tab-register').addEventListener('click', () => setAuthTab('register'));

  // Auth form submissions
  $('auth-form').addEventListener('submit', handleEmailAuth);

  // Google Login button
  $('google-login-btn').addEventListener('click', handleGoogleAuth);

  // Logout button
  $('logout-btn').addEventListener('click', handleSignOut);

  // Observe Firebase Auth State changes
  auth.onAuthStateChanged(async (firebaseUser) => {
    if (firebaseUser) {
      state.user = firebaseUser;
      state.idToken = await firebaseUser.getIdToken();

      // Show user information in Sidebar Footer
      const initials = firebaseUser.displayName ? 
                       firebaseUser.displayName.split(' ').map(n => n[0]).join('').slice(0, 2) : 
                       (firebaseUser.email ? firebaseUser.email.slice(0, 2) : '?');

      $('user-avatar').textContent = initials;
      $('user-display-name').textContent = firebaseUser.displayName || 'Team Member';
      $('user-display-email').textContent = firebaseUser.email;
      $('user-profile-section').style.display = 'flex';

      // Hide auth overlay and unlock app with a premium slide-out animation
      $('auth-overlay').style.opacity = '0';
      setTimeout(() => {
        $('auth-overlay').style.display = 'none';
      }, 500);

      showToast(`Welcome back, ${firebaseUser.displayName || firebaseUser.email}!`, 'success');
      await refreshAdminAccess();
      updateAdminNavigation();
    } else {
      state.user = null;
      state.idToken = null;
      state.admin.users = [];
      state.admin.settings = null;
      state.admin.modelStatus = null;

      try {
        const res = await fetch(`${API}/admin/session`, { method: 'GET' });
        const body = await res.json();
        state.admin.isAdmin = body.is_admin === true;
      } catch (err) {
        state.admin.isAdmin = false;
      }

      if (state.admin.isAdmin) {
        $('user-profile-section').style.display = 'none';
        updateAdminNavigation();
        return;
      }

      // Reset and hide Sidebar Profile
      $('user-profile-section').style.display = 'none';
      updateAdminNavigation();

      // Show Login overlay
      $('auth-overlay').style.display = 'flex';
      $('auth-overlay').style.opacity = '1';
    }
  });
}

function setAuthTab(tab) {
  activeAuthTab = tab;
  $('tab-login').classList.toggle('active', tab === 'login');
  $('tab-register').classList.toggle('active', tab === 'register');

  if (tab === 'login') {
    $('auth-subtitle').textContent = 'Sign in to access your Churn Prediction Dashboard';
    $('submit-btn-text').textContent = 'Sign In';
  } else {
    $('auth-subtitle').textContent = 'Create a secure account to deploy and use ChurnSight';
    $('submit-btn-text').textContent = 'Create Account';
  }
}

async function handleEmailAuth(e) {
  e.preventDefault();
  const email = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const btn = $('auth-submit-btn');

  btn.disabled = true;
  const originalText = $('submit-btn-text').textContent;
  $('submit-btn-text').innerHTML = `<span class="spinner-inline"></span> ${activeAuthTab === 'login' ? 'Signing In...' : 'Registering...'}`;

  try {
    if (activeAuthTab === 'login') {
      await auth.signInWithEmailAndPassword(email, password);
    } else {
      await auth.createUserWithEmailAndPassword(email, password);
    }
  } catch (err) {
    showToast(err.message, 'error');
    btn.disabled = false;
    $('submit-btn-text').textContent = originalText;
  }
}

async function handleGoogleAuth() {
  const provider = new firebase.auth.GoogleAuthProvider();
  try {
    await auth.signInWithPopup(provider);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function refreshAdminAccess() {
  if (state.user) {
    try {
      const token = await state.user.getIdToken(true);
      const res = await fetch(`${API}/admin/whoami`, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${token}`
        }
      });
      const body = await res.json();
      state.admin.isAdmin = body.is_admin === true;
      if (state.admin.isAdmin && $('section-admin').classList.contains('active')) {
        loadAdminPanel();
      }
      updateAdminNavigation();
      return;
    } catch (err) {
      // Continue to session-based fallback below
    }
  }

  try {
    const res = await fetch(`${API}/admin/session`, { method: 'GET' });
    const body = await res.json();
    state.admin.isAdmin = body.is_admin === true;
    if (state.admin.isAdmin && $('section-admin').classList.contains('active')) {
      loadAdminPanel();
    }
  } catch (err) {
    state.admin.isAdmin = false;
  }
  updateAdminNavigation();
}

async function loadAdminPanel() {
  if (!state.admin.isAdmin) return;
  await Promise.all([
    loadAdminUsers(),
    loadAdminSettings(),
    loadAdminModelStatus()
  ]);
}

async function loadAdminUsers() {
  const list = $('admin-user-list');
  list.innerHTML = '<tr><td class="empty-row" colspan="5">Loading users…</td></tr>';
  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/users`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    const body = await res.json();
    if (!res.ok) {
      list.innerHTML = `<tr><td class="empty-row" colspan="5">${body.error || 'Unable to fetch users'}</td></tr>`;
      return;
    }
    state.admin.users = body.users || [];
    renderAdminUsers();
  } catch (err) {
    list.innerHTML = `<tr><td class="empty-row" colspan="5">${err.message}</td></tr>`;
  }
}

function renderAdminUsers() {
  const list = $('admin-user-list');
  const query = $('admin-user-search')?.value?.toLowerCase() || '';
  const users = state.admin.users.filter(u => {
    return !query || [u.email, u.display_name, u.uid].some(value => value && value.toLowerCase().includes(query));
  });

  if (!users.length) {
    list.innerHTML = '<tr><td class="empty-row" colspan="5">No users found.</td></tr>';
    return;
  }

  list.innerHTML = users.map(user => {
    const providers = user.provider_ids.length ? user.provider_ids.join(', ') : 'Email';
    const status = user.disabled ? 'Disabled' : 'Active';
    const actionButton = user.disabled ? 'Enable' : 'Disable';
    const action = user.disabled ? 'enable' : 'disable';
    return `
      <tr>
        <td>${user.display_name || '—'}</td>
        <td>${user.email || '—'}</td>
        <td>${providers}</td>
        <td>${status}</td>
        <td class="table-actions">
          <button class="btn-secondary btn-small" data-action="${action}" data-uid="${user.uid}">${actionButton}</button>
          <button class="btn-danger btn-small" data-action="delete" data-uid="${user.uid}">Delete</button>
        </td>
      </tr>`;
  }).join('');
}

async function modifyAdminUser(uid, action) {
  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/users/${uid}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify({ action })
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to update user.', 'error');
      return;
    }
    showToast(`User ${action}d successfully.`, 'success');
    await loadAdminUsers();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function loadAdminSettings() {
  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/settings`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to load settings.', 'error');
      return;
    }
    state.admin.settings = body;
    $('admin-threshold-high').value = body.risk_thresholds.high;
    $('admin-threshold-medium').value = body.risk_thresholds.medium;
    $('admin-alert-reengage').checked = body.alert_rules.find(r => r.id === 'reengagement')?.enabled ?? false;
    $('admin-alert-support').checked = body.alert_rules.find(r => r.id === 'support_escalation')?.enabled ?? false;
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function saveAdminSettings() {
  const payload = {
    risk_thresholds: {
      high: parseFloat($('admin-threshold-high').value) || 0.7,
      medium: parseFloat($('admin-threshold-medium').value) || 0.4
    },
    alert_rules: [
      { id: 'reengagement', enabled: $('admin-alert-reengage').checked },
      { id: 'support_escalation', enabled: $('admin-alert-support').checked }
    ]
  };

  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/settings`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify(payload)
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to save settings.', 'error');
      return;
    }
    state.admin.settings = body;
    showToast('Admin settings saved.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function loadAdminModelStatus() {
  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/model`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to load model status.', 'error');
      return;
    }
    state.admin.modelStatus = body;
    $('admin-user-count').textContent = state.admin.users.length;
    $('admin-customer-count').textContent = body.customer_count ?? '—';
    $('admin-model-status').textContent = body.trained ? 'Trained' : (body.has_model ? 'Loaded' : 'None');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function adminModelAction(action) {
  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/model/actions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify({ action })
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to perform model action.', 'error');
      return;
    }
    showToast(`Model action ${action} completed.`, 'success');
    await loadAdminModelStatus();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function handleSignOut() {
  try {
    await auth.signOut();
    showToast('Signed out successfully.', 'success');
    // Reload to clear app state for safety
    location.reload();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/* ══════════════════  ADMIN EVENT BINDINGS ═══════════════════════ */
$('admin-refresh-btn')?.addEventListener('click', loadAdminPanel);
$('admin-reset-model-btn')?.addEventListener('click', () => adminModelAction('reset'));
$('admin-load-sample-btn')?.addEventListener('click', () => adminModelAction('reload_sample'));
$('admin-save-settings-btn')?.addEventListener('click', saveAdminSettings);
$('admin-user-search')?.addEventListener('input', renderAdminUsers);
$('admin-user-table')?.addEventListener('click', e => {
  const button = e.target.closest('button[data-uid]');
  if (!button) return;
  const uid = button.dataset.uid;
  const action = button.dataset.action;
  if (uid && action) {
    modifyAdminUser(uid, action);
  }
});

/* ══════════════════  HEALTH SCORES  ══════════════════════════ */
const HS_PAGE_SIZE = 9;

/* ── Score formula ──────────────────────────────────────────── */
function computeHealthScore(c) {
  // Each component 0-100; total is weighted mean
  const tenureScore    = Math.min(100, (c.tenure / 36) * 100);          // 0–36 months → 0-100
  const engageScore    = Math.min(100, (c.logins ?? 10) / 25 * 100);    // logins last 30 days
  const supportScore   = Math.max(0, 100 - (c.support ?? 0) * 18);      // fewer tickets = better
  const spendScore     = Math.min(100, (c.monthly ?? 50) / 120 * 100);  // spend up to $120
  const loyaltyScore   = Math.max(0, 100 - (c.last_login ?? 5) * 3);   // days since login

  // Weight: Tenure 25% | Engagement 25% | Support 20% | Spend 15% | Loyalty 15%
  const total = (tenureScore * 0.25) + (engageScore * 0.25) +
                (supportScore * 0.20) + (spendScore * 0.15) +
                (loyaltyScore * 0.15);

  // Churn probability subtracts up to 30 pts
  const churnPenalty = (c.churn_prob / 100) * 30;
  const final = Math.max(0, Math.min(100, total - churnPenalty));

  return {
    total       : Math.round(final),
    tenure      : Math.round(tenureScore),
    engagement  : Math.round(engageScore),
    support     : Math.round(supportScore),
    spend       : Math.round(spendScore),
    loyalty     : Math.round(loyaltyScore)
  };
}

function deriveTrend(c) {
  // Derive a pseudo-trend from tenure + support signals
  if (c.tenure >= 18 && (c.support ?? 0) <= 1 && c.churn_prob < 35) return 'improving';
  if (c.churn_prob > 60 || (c.support ?? 0) >= 4) return 'declining';
  return 'stable';
}

function buildDragDown(score) {
  const reasons = [];
  if (score.support < 40) reasons.push('High support ticket volume dragging health down');
  if (score.loyalty < 40) reasons.push('Extended inactivity reducing loyalty score');
  if (score.engagement < 30) reasons.push('Low login frequency signals disengagement');
  if (score.tenure < 25)   reasons.push('Short customer tenure adds uncertainty');
  return reasons[0] || null;
}

function buildRecs(c) {
  const recs = [];
  if (c.churn_prob >= 70)  recs.push('Schedule urgent executive business review');
  if (c.support >= 4)      recs.push('Escalate open tickets and assign a dedicated CSM');
  if (c.last_login >= 14)  recs.push('Send personalised re-engagement campaign');
  if (c.tenure <= 3)       recs.push('Enrol in onboarding success programme');
  if (c.monthly <= 30)     recs.push('Offer upsell / expansion conversation');
  if (recs.length === 0)   recs.push('Continue standard success check-in cadence');
  return recs.slice(0, 3);
}

/* ── Compute all scores once training completes ─────────────── */
function buildHealthScores() {
  hsState.scores = state.allCustomers.map(c => {
    const score = computeHealthScore(c);
    return {
      id        : c.id,
      score     : score.total,
      components: score,
      trend     : deriveTrend(c),
      risk      : c.risk_level,
      churn_prob: c.churn_prob,
      tenure    : c.tenure,
      monthly   : c.monthly,
      support   : c.support,
      last_login: c.last_login,
      logins    : c.logins,
      actual    : c.actual,
      recs      : buildRecs(c),
      dragDown  : buildDragDown(score)
    };
  });
}

/* ── Filter + Sort ──────────────────────────────────────────── */
function applyHsFilters() {
  let data = [...hsState.scores];
  if (hsState.risk !== 'all')  data = data.filter(c => c.risk === hsState.risk);
  if (hsState.trend !== 'all') data = data.filter(c => c.trend === hsState.trend);

  switch (hsState.sort) {
    case 'score_asc':   data.sort((a, b) => a.score - b.score); break;
    case 'score_desc':  data.sort((a, b) => b.score - a.score); break;
    case 'prob_desc':   data.sort((a, b) => b.churn_prob - a.churn_prob); break;
    case 'tenure_desc': data.sort((a, b) => b.tenure - a.tenure); break;
  }
  hsState.filtered = data;
  hsState.page = 1;
}

/* ── KPI summary strip ──────────────────────────────────────── */
function updateHsKpis() {
  const data = hsState.filtered;
  if (!data.length) {
    ['hs-kpi-avg','hs-kpi-critical','hs-kpi-at-risk','hs-kpi-healthy','hs-kpi-improving']
      .forEach(id => { if ($(id)) $(id).textContent = '–'; });
    return;
  }
  const avg       = Math.round(data.reduce((s, c) => s + c.score, 0) / data.length);
  const critical  = data.filter(c => c.score < 40).length;
  const atRisk    = data.filter(c => c.score >= 40 && c.score <= 65).length;
  const healthy   = data.filter(c => c.score > 65).length;
  const improving = data.filter(c => c.trend === 'improving').length;

  $('hs-kpi-avg').textContent       = avg;
  $('hs-kpi-critical').textContent  = critical;
  $('hs-kpi-at-risk').textContent   = atRisk;
  $('hs-kpi-healthy').textContent   = healthy;
  $('hs-kpi-improving').textContent = improving;
}

/* ── Score ring mini-chart ──────────────────────────────────── */
function drawHsRing(canvas, score) {
  if (!canvas) return;
  // Destroy any previous Chart.js instance on this canvas
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  const color = score >= 66 ? '#34d399' : score >= 40 ? '#fb923c' : '#f87171';
  new Chart(canvas.getContext('2d'), {
    type: 'doughnut',
    data: {
      datasets: [{
        data: [score, 100 - score],
        backgroundColor: [color, 'rgba(255,255,255,0.05)'],
        borderWidth: 0,
        hoverOffset: 0
      }]
    },
    options: {
      responsive: false,
      cutout: '76%',
      animation: { animateRotate: true, duration: 700 },
      plugins: { legend: { display: false }, tooltip: { enabled: false } }
    }
  });
}

/* ── Trend icon HTML ────────────────────────────────────────── */
function trendIcon(trend) {
  if (trend === 'improving')
    return `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 11 7 5 11 9 14 6"/></svg>`;
  if (trend === 'declining')
    return `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 5 7 11 11 7 14 10"/></svg>`;
  return `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><line x1="2" y1="8" x2="14" y2="8"/></svg>`;
}

/* ── Render one health card ─────────────────────────────────── */
function renderHsCard(c) {
  const { components: sc } = c;
  const comps = [
    { label: 'Tenure',      val: sc.tenure },
    { label: 'Engagement',  val: sc.engagement },
    { label: 'Support',     val: sc.support },
    { label: 'Spend',       val: sc.spend },
    { label: 'Loyalty',     val: sc.loyalty }
  ];
  const fillColor = c.score >= 66 ? '#34d399' : c.score >= 40 ? '#fb923c' : '#f87171';
  const canvasId = `hs-ring-${c.id.replace(/\s+/g,'_')}`;

  const dragHtml = c.dragDown
    ? `<div class="hs-drag-down">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        ${c.dragDown}
      </div>`
    : '';

  const recsHtml = c.recs.map(r => `<li>${r}</li>`).join('');

  const metaItems = [
    { label: 'Monthly Spend', val: `$${(c.monthly||0).toFixed(0)}` },
    { label: 'Support Tickets', val: c.support ?? 0 },
    { label: 'Last Login',      val: `${c.last_login ?? 0} days ago` },
    { label: 'Actual Churn',    val: c.actual === 1 ? '● Churned' : '● Retained' }
  ];

  return `
    <div class="hs-card tier-${c.risk}" data-id="${c.id}" id="hsc-${c.id.replace(/\s+/g,'_')}">
      <!-- Header -->
      <div class="hs-card-header">
        <span class="hs-card-id">${c.id}</span>
        <div class="hs-card-badges">
          <span class="hs-tier-badge ${c.risk}">${c.risk}</span>
          <span class="hs-trend-badge ${c.trend}">${trendIcon(c.trend)} ${c.trend.charAt(0).toUpperCase()+c.trend.slice(1)}</span>
        </div>
      </div>

      <!-- Score Ring -->
      <div class="hs-score-row">
        <div class="hs-score-ring">
          <canvas id="${canvasId}" width="72" height="72"></canvas>
          <div class="hs-score-label">
            <span class="hs-score-num">${c.score}</span>
            <span class="hs-score-sub">score</span>
          </div>
        </div>
        <div class="hs-score-meta">
          <div class="hs-score-churn"><strong>${c.churn_prob}%</strong> churn prob.</div>
          <div class="hs-score-tenure">Tenure: ${c.tenure} mo</div>
        </div>
      </div>

      <!-- Component bars -->
      <div class="hs-components">
        ${comps.map(comp => `
          <div class="hs-comp-row">
            <div class="hs-comp-header"><span>${comp.label}</span><span>${comp.val}</span></div>
            <div class="hs-comp-bar">
              <div class="hs-comp-fill" style="width:${comp.val}%; background:${comp.val >= 60 ? '#34d399' : comp.val >= 35 ? '#fb923c' : '#f87171'};"></div>
            </div>
          </div>`).join('')}
      </div>

      ${dragHtml}

      <!-- Expand toggle -->
      <button class="hs-card-expand-btn" aria-expanded="false">
        View CSM Actions
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
      </button>

      <!-- Expandable detail -->
      <div class="hs-card-detail">
        <p class="hs-detail-title">Recommended Actions</p>
        <ul class="hs-detail-recs">${recsHtml}</ul>
        <p class="hs-detail-title">Customer Snapshot</p>
        <div class="hs-detail-meta">
          ${metaItems.map(m => `
            <div class="hs-detail-meta-item">
              <strong>${m.val}</strong>${m.label}
            </div>`).join('')}
        </div>
      </div>
    </div>`;
}

/* ── Render the full grid ───────────────────────────────────── */
function renderHsGrid() {
  const grid = $('hs-card-grid');
  if (!grid) return;

  const data  = hsState.filtered;
  const pages = Math.ceil(data.length / HS_PAGE_SIZE);
  const start = (hsState.page - 1) * HS_PAGE_SIZE;
  const paged = data.slice(start, start + HS_PAGE_SIZE);

  if (paged.length === 0) {
    grid.innerHTML = `<div class="hs-empty">
      <svg viewBox="0 0 64 64" fill="none"><circle cx="32" cy="32" r="28" fill="rgba(99,102,241,0.08)"/><line x1="20" y1="32" x2="44" y2="32" stroke="#818cf8" stroke-width="3" stroke-linecap="round"/></svg>
      <p>No customers match these filters.</p>
    </div>`;
    $('hs-pagination').innerHTML = '';
    return;
  }

  grid.innerHTML = paged.map(renderHsCard).join('');

  // Draw rings after DOM insert
  requestAnimationFrame(() => {
    paged.forEach(c => {
      const canvasId = `hs-ring-${c.id.replace(/\s+/g,'_')}`;
      const canvas   = document.getElementById(canvasId);
      if (canvas) drawHsRing(canvas, c.score);
    });
  });

  // Expand/collapse cards
  grid.querySelectorAll('.hs-card-expand-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const card = btn.closest('.hs-card');
      const isExp = card.classList.toggle('expanded');
      btn.setAttribute('aria-expanded', isExp);
    });
  });

  // Whole card click also toggles
  grid.querySelectorAll('.hs-card').forEach(card => {
    card.addEventListener('click', () => {
      const isExp = card.classList.toggle('expanded');
      const btn   = card.querySelector('.hs-card-expand-btn');
      if (btn) btn.setAttribute('aria-expanded', isExp);
    });
  });

  renderHsPagination(pages);
}

/* ── Pagination ─────────────────────────────────────────────── */
function renderHsPagination(pages) {
  const wrap = $('hs-pagination');
  if (!wrap || pages <= 1) { if (wrap) wrap.innerHTML = ''; return; }

  let html = `<button class="page-btn" ${hsState.page===1?'disabled':''} data-hp="${hsState.page-1}">‹ Prev</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 3 && i < pages - 1 && Math.abs(i - hsState.page) > 1) {
      if (i === 4) html += '<span style="color:var(--text-3);padding:0 4px">…</span>';
      continue;
    }
    html += `<button class="page-btn ${i===hsState.page?'active':''}" data-hp="${i}">${i}</button>`;
  }
  html += `<button class="page-btn" ${hsState.page===pages?'disabled':''} data-hp="${hsState.page+1}">Next ›</button>`;
  wrap.innerHTML = html;

  wrap.querySelectorAll('.page-btn[data-hp]').forEach(btn => {
    btn.addEventListener('click', () => {
      hsState.page = parseInt(btn.dataset.hp);
      renderHsGrid();
    });
  });
}

/* ── Full refresh ───────────────────────────────────────────── */
function refreshHealthView() {
  if (!state.modelTrained || !hsState.scores.length) return;
  applyHsFilters();
  updateHsKpis();
  renderHsGrid();
}

/* ── Wire up filters ────────────────────────────────────────── */
document.querySelectorAll('#hs-trend-filters .hs-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#hs-trend-filters .hs-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    hsState.trend = btn.dataset.trend;
    refreshHealthView();
  });
});
document.querySelectorAll('#hs-risk-filters .hs-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#hs-risk-filters .hs-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    hsState.risk = btn.dataset.risk;
    refreshHealthView();
  });
});
$('hs-sort')?.addEventListener('change', () => {
  hsState.sort = $('hs-sort').value;
  refreshHealthView();
});

/* ══════════════════  INIT  ════════════════════════════════ */
initAuthUI();
navigateTo('upload');
