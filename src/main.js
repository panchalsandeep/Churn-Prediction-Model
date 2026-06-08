/* ═══════════════════════════════════════════════════════════════
   ChurnSight  –  Frontend Application (Modular Firebase SDK)
═══════════════════════════════════════════════════════════════ */

// ── Firebase Modular SDK Imports ─────────────────────────────────────────────
import { initializeApp } from 'firebase/app';
import { getAnalytics } from 'firebase/analytics';
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signInWithPopup,
  GoogleAuthProvider,
  signOut,
} from 'firebase/auth';

// ── Firebase Configuration ───────────────────────────────────────────────────
const firebaseConfig = {
  apiKey: "AIzaSyBdsEwt7eAmBjtGDk_ns68QSrTK77mpWkQ",
  authDomain: "churn-prediction-model-ai.firebaseapp.com",
  projectId: "churn-prediction-model-ai",
  storageBucket: "churn-prediction-model-ai.firebasestorage.app",
  messagingSenderId: "24302908104",
  appId: "1:24302908104:web:8a907a48ea41ccbbad6bfd",
  measurementId: "G-SP7GXCV9J2"
};

// ── Initialize Firebase ──────────────────────────────────────────────────────
const firebaseApp = initializeApp(firebaseConfig);
const analytics   = getAnalytics(firebaseApp);
const auth        = getAuth(firebaseApp);

// ── API Base URL ─────────────────────────────────────────────────────────────
const API = '/api';

/* ── State ──────────────────────────────────────────────────── */
let state = {
  modelTrained      : false,
  algorithm         : 'random_forest',
  selectedFile      : null,
  results           : null,
  allCustomers      : [],
  filteredCustomers : [],
  currentPage       : 1,
  pageSize          : 20,
  riskFilter        : 'All',
  healthFilter      : 'All',
  alertTab          : 'Open',
  cohortType        : 'signup',
  alerts            : [],
  cohorts           : null,
  searchQuery       : '',
  charts            : {},
  user              : null,
  idToken           : null,
  admin             : {
    isAdmin: false,
    users: [],
    settings: null,
    modelStatus: null
  }
};

/* ── DOM helpers ─────────────────────────────────────────────── */
const $  = id => document.getElementById(id);
const qs = s  => document.querySelector(s);

/* ══════════════════  NAVIGATION  ═════════════════════════════ */
const sections = ['upload', 'overview', 'health', 'cohorts', 'alerts', 'analytics', 'customers', 'predict', 'admin'];
const titles   = {
  upload   : 'Upload & Train Model',
  overview : 'Overview Dashboard',
  health   : 'Customer Health Scores',
  cohorts  : 'Cohort Analysis',
  alerts   : 'Alerts & Playbooks',
  analytics: 'Analytics & Model Performance',
  customers: 'Customer Risk List',
  predict  : 'Predict Single Customer',
  admin    : 'Admin Console'
};

function navigateTo(section) {
  sections.forEach(s => {
    const navItem = $(`nav-${s}`);
    $(`section-${s}`).classList.toggle('active', s === section);
    navItem.classList.toggle('active', s === section);
    if (s === section) {
      navItem.setAttribute('aria-current', 'page');
    } else {
      navItem.removeAttribute('aria-current');
    }
  });
  $('page-title').textContent = titles[section];

  if (section === 'admin') {
    loadAdminPanel();
  }

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
    if (!state.modelTrained && sec !== 'upload' && sec !== 'predict' && sec !== 'admin') {
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
  $('time-display').textContent = new Date().toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}
updateClock();
setInterval(updateClock, 1000);

/* ══════════════════  FILE UPLOAD  ══════════════════════════════ */
const dropZone  = $('drop-zone');
const fileInput = $('file-input');

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragging'); });
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

    // Refresh token before request
    if (state.user) {
      state.idToken = await state.user.getIdToken(true);
    }

    const headers = {};
    if (state.idToken) headers['Authorization'] = `Bearer ${state.idToken}`;

    const res  = await fetch(`${API}/upload`, { method: 'POST', headers, body: fd });
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

    state.results      = data;
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

  animateValue('kpi-total-val', 0, summary.total_customers, 800);
  $('kpi-churn-val').textContent = summary.churn_rate + '%';
  animateValue('kpi-high-val', 0, summary.high_risk, 800);
  $('kpi-acc-val').textContent  = metrics.accuracy  + '%';
  $('kpi-f1-val').textContent   = metrics.f1_score  + '%';
  $('kpi-auc-val').textContent  = metrics.auc_roc   + '%';

  const badge = $('algo-badge');
  badge.textContent   = summary.algorithm;
  badge.style.display = 'flex';

  buildTrendChart(td);
  buildRiskDonut(summary);
  buildProbDistChart(pd);
  buildROCChart(roc);
  buildFeatureChart(fi);
  buildRingCharts(metrics);

  $('cm-tn').textContent = cm[0][0];
  $('cm-fp').textContent = cm[0][1];
  $('cm-fn').textContent = cm[1][0];
  $('cm-tp').textContent = cm[1][1];

  $('ring-acc-val').textContent  = metrics.accuracy  + '%';
  $('ring-prec-val').textContent = metrics.precision + '%';
  $('ring-rec-val').textContent  = metrics.recall    + '%';
  $('ring-f1-val').textContent   = metrics.f1_score  + '%';

  state.filteredCustomers = [...state.allCustomers];
  renderCustomerTable();
  renderHealthScores();
  buildCohortData();
  renderCohortView();
  initAlerts();
}

/* ══════════════════  CHART BUILDERS  ══════════════════════════ */
Chart.defaults.color       = '#94a3b8';
Chart.defaults.font.family = 'Inter';

function destroyChart(key) {
  if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
}

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
          tension: 0.4, fill: true,
          pointBackgroundColor: '#f87171', pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5,
        },
        {
          label: 'Retention %',
          data: td.retained,
          borderColor: '#34d399',
          backgroundColor: 'rgba(52,211,153,0.06)',
          tension: 0.4, fill: true,
          pointBackgroundColor: '#34d399', pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, pointStyleWidth: 10, padding: 18 } },
        tooltip: tooltipStyle(),
      },
      scales: {
        x: gridStyle(),
        y: { ...gridStyle(), min: 0, max: 100, ticks: { callback: v => v + '%' } },
      },
    },
  });
}

function buildRiskDonut(summary) {
  const donutLegend = $('donut-legend');
  donutLegend.setAttribute('role', 'list');
  destroyChart('donut');
  const ctx    = $('riskDonutChart').getContext('2d');
  const labels = ['High Risk', 'Medium Risk', 'Low Risk'];
  const data   = [summary.high_risk, summary.medium_risk, summary.low_risk];
  const colors = ['#f87171', '#fb923c', '#34d399'];

  state.charts.donut = new Chart(ctx, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: 'transparent', hoverOffset: 8, borderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '72%',
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
    },
  });

  $('donut-legend').innerHTML = labels.map((l, i) => `
    <div class="legend-item" role="listitem">
      <div class="legend-dot" style="background:${colors[i]}"></div>
      <span>${l}: <b>${data[i]}</b></span>
    </div>`).join('');
}

function buildProbDistChart(pd) {
  destroyChart('probDist');
  const ctx      = $('probDistChart').getContext('2d');
  const bgColors = pd.labels.map((_, i) => {
    const pct = i / (pd.labels.length - 1);
    return `rgba(${Math.round(99 + pct * 155)},${Math.round(102 - pct * 80)},${Math.round(241 - pct * 170)},0.75)`;
  });

  state.charts.probDist = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: pd.labels.map(l => l + '%'),
      datasets: [{ label: 'Customers', data: pd.counts, backgroundColor: bgColors, borderRadius: 6, borderSkipped: false }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: { display: true, text: 'Churn Probability Range', color: '#64748b' } },
        y: { ...gridStyle(), title: { display: true, text: 'Number of Customers', color: '#64748b' } },
      },
    },
  });
}

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
          borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.08)',
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2.5,
        },
        {
          label: 'Random Classifier',
          data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
          borderColor: 'rgba(255,255,255,0.12)', borderDash: [6, 4],
          pointRadius: 0, borderWidth: 1.5, fill: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { usePointStyle: true, padding: 14 } }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), type: 'linear', min: 0, max: 1, title: { display: true, text: 'False Positive Rate', color: '#64748b' } },
        y: { ...gridStyle(), min: 0, max: 1, title: { display: true, text: 'True Positive Rate', color: '#64748b' } },
      },
    },
  });
}

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
        borderRadius: 6, borderSkipped: false,
      }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: { display: true, text: 'Importance Score', color: '#64748b' } },
        y: gridStyle(),
      },
    },
  });
}

function buildRingCharts(metrics) {
  const rings = [
    { canvas: 'ringAccCanvas',  value: metrics.accuracy,  color: '#34d399' },
    { canvas: 'ringPrecCanvas', value: metrics.precision, color: '#60a5fa' },
    { canvas: 'ringRecCanvas',  value: metrics.recall,    color: '#f59e0b' },
    { canvas: 'ringF1Canvas',   value: metrics.f1_score,  color: '#a78bfa' },
  ];
  rings.forEach(({ canvas, value, color }) => {
    destroyChart(canvas);
    const ctx = $(canvas).getContext('2d');
    state.charts[canvas] = new Chart(ctx, {
      type: 'doughnut',
      data: { datasets: [{ data: [value, 100 - value], backgroundColor: [color, 'rgba(255,255,255,0.05)'], borderWidth: 0, hoverOffset: 0 }] },
      options: {
        responsive: false, cutout: '78%',
        animation: { animateRotate: true, duration: 900 },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
      },
    });
  });
}

/* ══════════════════  GAUGE CHART  ════════════════════════════ */
function drawGauge(canvas, probability) {
  const ctx = $(canvas).getContext('2d');
  const W = 220, H = 130, cx = W / 2, cy = H - 18, r = 90;

  ctx.clearRect(0, 0, W, H);

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0, false);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();

  const pct  = probability / 100;
  const end  = Math.PI + pct * Math.PI;
  const grad = ctx.createLinearGradient(cx - r, cy, cx + r, cy);
  if (probability >= 70) {
    grad.addColorStop(0, '#fbbf24'); grad.addColorStop(1, '#f87171');
  } else if (probability >= 40) {
    grad.addColorStop(0, '#34d399'); grad.addColorStop(1, '#fbbf24');
  } else {
    grad.addColorStop(0, '#60a5fa'); grad.addColorStop(1, '#34d399');
  }

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, end, false);
  ctx.strokeStyle = grad; ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();

  const needleAngle = Math.PI + pct * Math.PI;
  const nx = cx + (r - 26) * Math.cos(needleAngle);
  const ny = cy + (r - 26) * Math.sin(needleAngle);
  ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(nx, ny);
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5; ctx.lineCap = 'round'; ctx.stroke();

  ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI * 2);
  ctx.fillStyle = '#fff'; ctx.fill();

  ctx.font = '500 10px Inter'; ctx.fillStyle = 'rgba(255,255,255,0.4)'; ctx.textAlign = 'center';
  ctx.fillText('0%',   cx - r + 4, cy + 18);
  ctx.fillText('50%',  cx,          cy - r - 8);
  ctx.fillText('100%', cx + r - 4, cy + 18);
}

/* ══════════════════  CUSTOMER TABLE  ════════════════════════ */
function renderCustomerTable() {
  let data = [...state.allCustomers];
  if (state.riskFilter !== 'All') data = data.filter(c => c.risk_level === state.riskFilter);
  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    data = data.filter(c => c.id.toLowerCase().includes(q));
  }

  state.filteredCustomers = data;
  const total = data.length;
  const pages = Math.ceil(total / state.pageSize);
  const start = (state.currentPage - 1) * state.pageSize;
  const paged = data.slice(start, start + state.pageSize);

  $('customer-count').textContent = `Showing ${paged.length} of ${total} customers`;

  const tbody = $('customer-tbody');
  if (!paged.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No customers match the current filters.</td></tr>';
  } else {
    tbody.innerHTML = paged.map(c => {
      const fillClass = c.risk_level === 'High' ? 'fill-high' : c.risk_level === 'Medium' ? 'fill-medium' : 'fill-low';
      return `
        <tr class="customer-row" data-customer-id="${c.id}">
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
    tbody.querySelectorAll('.customer-row').forEach(row => {
      row.addEventListener('click', () => showCustomerDrawer(row.dataset.customerId));
    });
  }
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

document.querySelectorAll('#risk-filters .risk-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#risk-filters .risk-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.riskFilter  = btn.dataset.risk;
    state.currentPage = 1;
    renderCustomerTable();
  });
});

document.querySelectorAll('#health-risk-filters .risk-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#health-risk-filters .risk-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.healthFilter = btn.dataset.filter;
    renderHealthScores();
  });
});

$('customer-search').addEventListener('input', e => {
  state.searchQuery = e.target.value.trim();
  state.currentPage = 1;
  renderCustomerTable();
});
$('retrain-btn').addEventListener('click', () => {
  navigateTo('upload');
  showToast('Ready to retrain your model.', 'success');
});

document.querySelectorAll('.kpi-card[data-risk]').forEach(card => {
  card.addEventListener('click', () => {
    state.riskFilter = card.dataset.risk;
    state.currentPage = 1;
    document.querySelectorAll('#risk-filters .risk-filter').forEach(b => b.classList.toggle('active', b.dataset.risk === state.riskFilter));
    renderCustomerTable();
    navigateTo('customers');
  });
});

$('cohort-type').addEventListener('change', e => {
  state.cohortType = e.target.value;
  buildCohortData();
  renderCohortView();
});

document.querySelectorAll('#alert-tabs .alert-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    setAlertTab(tab.dataset.tab);
  });
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
    age             : +$('p-age').value,
    gender          : $('p-gender').value,
    tenure_months   : +$('p-tenure').value,
    monthly_charges : +$('p-monthly').value,
    total_charges   : +$('p-total').value,
    num_products    : +$('p-products').value,
    num_logins_last30: +$('p-logins').value,
    support_tickets : +$('p-tickets').value,
    last_login_days : +$('p-lastlogin').value,
    location        : $('p-location').value,
    contract_type   : $('p-contract').value,
    payment_method  : $('p-payment').value,
  };

  try {
    if (state.user) state.idToken = await state.user.getIdToken(true);
    const headers = { 'Content-Type': 'application/json' };
    if (state.idToken) headers['Authorization'] = `Bearer ${state.idToken}`;

    const res  = await fetch(`${API}/predict`, { method: 'POST', headers, body: JSON.stringify(payload) });
    const data = await res.json();

    if (!res.ok) { showToast(data.error || 'Prediction failed.', 'error'); return; }
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

  const { churn_probability: prob, risk_level: risk, recommendation: recs = [] } = data;

  drawGauge('gaugeCanvas', prob);
  $('gauge-val').textContent = prob + '%';

  const badge = $('risk-badge-large');
  badge.className = `risk-badge-large ${risk}`;
  $('risk-label-large').textContent = risk + ' Risk';

  $('recs-list').innerHTML = recs.map(r => `<li>${r}</li>`).join('');
  animateCounter('gauge-val', 0, prob, 900, v => v.toFixed(1) + '%');
}

/* ══════════════════  HELPERS  ══════════════════════════════ */
function tooltipStyle() {
  return {
    backgroundColor: 'rgba(15,19,34,0.95)', borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1,
    titleFont: { weight: '700' }, bodyFont: { size: 12 }, padding: 10, cornerRadius: 8,
  };
}

function gridStyle() {
  return {
    grid  : { color: 'rgba(255,255,255,0.05)' },
    ticks : { color: '#64748b', font: { size: 11 } },
    border: { color: 'transparent' },
  };
}

function showLoading(text = 'Processing…') {
  $('loading-text').textContent = text;
  $('loading-overlay').style.display = 'flex';
}
function hideLoading() {
  $('loading-overlay').style.display = 'none';
  $('progress-wrap').style.display   = 'none';
}
function showProgress(pct, label) {
  $('progress-wrap').style.display  = 'block';
  $('progress-fill').style.width    = pct + '%';
  $('progress-label').textContent   = label;
}
function showToast(msg, type = 'success') {
  const t = $('toast');
  t.textContent = msg;
  t.className   = `toast ${type} show`;
  setTimeout(() => t.classList.remove('show'), 3500);
}
function updateModelStatus(algo) {
  $('model-status-badge').innerHTML =
    `<span class="status-dot active"></span><span>${algo} Ready</span>`;
}

function getCustomerById(id) {
  return state.allCustomers.find(c => c.id === id);
}

function showCustomerDrawer(id) {
  const customer = getCustomerById(id);
  if (!customer) return;
  $('drawer-customer-id').textContent = customer.id;
  $('drawer-risk-tag').textContent = customer.risk_level;
  $('drawer-risk-tag').className = `risk-tag ${customer.risk_level}`;
  $('drawer-churn-prob').textContent = `${customer.churn_prob}%`;
  $('drawer-tenure').textContent = `${customer.tenure} mo`;
  $('drawer-monthly').textContent = `$${customer.monthly.toFixed(2)}`;
  $('drawer-support').textContent = customer.support;
  $('drawer-last-login').textContent = `${customer.last_login} days`;
  $('drawer-contract').textContent = customer.contract_type || 'N/A';
  $('drawer-payment').textContent = customer.payment_method || 'N/A';
  $('drawer-action').textContent = customer.risk_level === 'High'
    ? 'Prioritize outreach and service recovery for this customer.'
    : 'Continue normal customer success playbook.';
  $('customer-drawer').style.display = 'flex';
}

function closeCustomerDrawer() {
  $('customer-drawer').style.display = 'none';
}

function calculateHealthScore(customer) {
  const base = Math.max(18, Math.min(100, 100 - customer.churn_prob));
  const engagement = Math.max(10, Math.min(100, 100 - customer.last_login * 2));
  const support = Math.max(8, Math.min(100, 100 - customer.support * 10));
  const stability = customer.monthly > 80 ? 70 : 85;
  const score = Math.round((base * 0.45) + (engagement * 0.25) + (support * 0.2) + (stability * 0.1));
  return {
    score,
    components: [
      { name: 'Model Score', value: Math.round(base) },
      { name: 'Engagement', value: Math.round(engagement) },
      { name: 'Support Health', value: Math.round(support) },
      { name: 'Payment Stability', value: Math.round(stability) },
    ],
  };
}

function renderHealthScores() {
  const list = state.allCustomers.map(c => ({ ...c, ...calculateHealthScore(c) }));
  const filtered = state.healthFilter === 'All' ? list : list.filter(c => c.risk_level === state.healthFilter);
  const top = filtered.sort((a,b) => b.score - a.score).slice(0, 9);
  const average = filtered.length ? Math.round(filtered.reduce((sum,x) => sum + x.score, 0)/filtered.length) : 0;
  const critical = filtered.filter(c => c.score < 40).length;
  const improving = filtered.filter(c => c.score >= 70 && c.risk_level !== 'High').length;

  $('health-avg-score').textContent = top.length ? `${average}` : '–';
  $('health-critical-count').textContent = critical;
  $('health-improving-count').textContent = improving;

  const container = $('health-grid');
  if (!top.length) {
    container.innerHTML = '<div class="health-card empty-card"><p>No health score data available yet. Train your model to populate this screen.</p></div>';
    return;
  }

  container.innerHTML = top.map(customer => {
    const tier = customer.score >= 75 ? 'Low' : customer.score >= 45 ? 'Medium' : 'High';
    const worst = [...customer.components].sort((a,b) => a.value - b.value).slice(0, 2);
    return `
      <div class="health-card">
        <div class="health-card-header">
          <h4>${customer.id}</h4>
          <span class="health-tier ${tier}">${tier} Risk</span>
        </div>
        <div class="health-score">${customer.score}</div>
        <div class="health-note">${customer.score < 45 ? 'Urgent action required to retain this account.' : 'Monitor key signals and engage proactively.'}</div>
        ${customer.components.map(comp => `
          <div class="health-component">
            <span>${comp.name}</span>
            <span>${comp.value}%</span>
          </div>
          <div class="health-bar"><div class="health-bar-fill" style="width:${comp.value}%"></div></div>
        `).join('')}
        <div class="health-note"><strong>Drag factor:</strong> ${worst.map(c => c.name).join(' & ')}</div>
      </div>`;
  }).join('');
}

function buildCohortData() {
  if (!state.allCustomers.length) return;
  const buckets = {};
  state.allCustomers.forEach(customer => {
    let key = 'Unknown';
    if (state.cohortType === 'contract') key = customer.contract_type || 'Unknown';
    else if (state.cohortType === 'location') key = customer.location || 'Unknown';
    else key = `Cohort ${((parseInt(customer.id.replace(/\D/g, ''), 10) || 1) % 6) + 1}`;
    buckets[key] = buckets[key] || [];
    buckets[key].push(customer);
  });

  const cohorts = Object.entries(buckets).slice(0, 6);
  const months = ['M0','M1','M2','M3','M4','M5'];
  state.cohorts = {
    labels: cohorts.map(([label]) => label),
    months,
    heatmap: cohorts.map(([, customers]) => {
      const avgChurn = customers.length ? customers.reduce((sum, c) => sum + c.churn_prob, 0) / customers.length : 0;
      const base = customers.length ? 90 - avgChurn * 0.3 : 75;
      return months.map((_, index) => Math.max(18, Math.min(100, Math.round(base - index * 10 + Math.random() * 6))));
    }),
    contractCounts: cohorts.reduce((acc, [label, customers]) => {
      acc[label] = customers.length;
      return acc;
    }, {}),
    totalCohorts: cohorts.length,
    totalCustomers: state.allCustomers.length,
  };
}

function renderCohortView() {
  const container = $('heatmap-grid');
  if (!state.cohorts || !state.cohorts.labels.length) {
    container.innerHTML = '<div class="heatmap-empty">Train your model to generate cohort retention and contract analysis.</div>';
    $('cohort-summary').textContent = 'Train your model to display cohort segments and retention curves.';
    return;
  }
  const { labels, months, heatmap } = state.cohorts;
  container.innerHTML = '';
  labels.forEach((label, rowIndex) => {
    months.forEach((month, monthIndex) => {
      const value = heatmap[rowIndex][monthIndex];
      const bg = value >= 85 ? 'rgba(16,185,129,0.32)' : value >= 70 ? 'rgba(16,185,129,0.2)' : value >= 55 ? 'rgba(245,158,11,0.18)' : value >= 40 ? 'rgba(239,68,68,0.16)' : 'rgba(239,68,68,0.28)';
      const color = value >= 70 ? '#047857' : value >= 55 ? '#92400E' : '#7F1D1D';
      container.innerHTML += `<div class="heatmap-cell" style="background:${bg}; color:${color};">${label} ${month}<br/><strong>${value}%</strong></div>`;
    });
  });

  const labelType = state.cohortType === 'contract' ? 'Contract Type' : state.cohortType === 'location' ? 'Location' : 'Signup Cohort';
  $('cohort-summary').textContent = `Displaying ${state.cohorts.totalCohorts} ${labelType} cohorts across ${state.cohorts.months.length} periods.`;

  buildCohortTrendChart();
  buildContractChart();
}

function buildCohortTrendChart() {
  destroyChart('cohortTrend');
  const ctx = $('cohortTrendChart').getContext('2d');
  const data = state.cohorts.labels.map((label, index) => ({ label, value: state.cohorts.heatmap[index][2] }));
  state.charts.cohortTrend = new Chart(ctx, {
    type: 'line',
    data: {
      labels: state.cohorts.months,
      datasets: state.cohorts.labels.map((label, index) => ({
        label,
        data: state.cohorts.heatmap[index],
        borderColor: `hsl(${index * 40}, 78%, 63%)`, fill: false, tension: 0.3, pointRadius: 3, borderWidth: 2.5,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { color: '#94a3b8' } }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: { display: true, text: 'Month', color: '#64748b' } },
        y: { ...gridStyle(), min: 0, max: 100, title: { display: true, text: 'Retention %', color: '#64748b' } },
      },
    },
  });
}

function buildContractChart() {
  destroyChart('contract');
  const ctx = $('contractChart').getContext('2d');
  const labels = Object.keys(state.cohorts.contractCounts);
  const values = Object.values(state.cohorts.contractCounts);
  state.charts.contract = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Customers', data: values,
        backgroundColor: labels.map((_, index) => `hsla(${index * 45}, 85%, 60%, 0.82)`),
        borderRadius: 6, borderSkipped: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle() },
        y: { ...gridStyle(), beginAtZero: true },
      },
    },
  });
}

function initAlerts() {
  state.alerts = generateAlerts();
  setAlertTab(state.alertTab);
}

function generateAlerts() {
  const highRisk = state.allCustomers.filter(c => c.risk_level === 'High').slice(0, 4);
  const mediumRisk = state.allCustomers.filter(c => c.risk_level === 'Medium').slice(0, 2);
  const open = highRisk.map((customer, idx) => ({
    id: `alert-open-${idx}`,
    customer: customer.id,
    severity: customer.risk_level,
    title: 'Churn probability rising',
    signals: ['Low activity', 'Support spike', 'Payment warning'],
    time: `${2 + idx}h ago`,
    status: 'Open',
    details: `Customer ${customer.id} has a churn probability of ${customer.churn_prob}%.`,
  }));
  const acknowledged = mediumRisk.map((customer, idx) => ({
    id: `alert-ack-${idx}`,
    customer: customer.id,
    severity: customer.risk_level,
    title: 'Engagement declined',
    signals: ['Login drop', 'Feature usage low'],
    time: `${10 + idx}h ago`,
    status: 'Acknowledged',
    details: `Assigned CSM follow-up for ${customer.id}.`,
  }));
  const resolved = [{
    id: 'alert-res-0',
    customer: state.allCustomers[0]?.id || 'C001',
    severity: 'Medium',
    title: 'Payment issue resolved',
    signals: ['Invoice paid', 'Support closed'],
    time: '1d ago',
    status: 'Resolved',
    details: 'Customer retention outreach completed successfully.',
  }];
  return { Open: open, Acknowledged: acknowledged, Resolved: resolved };
}

function setAlertTab(tab) {
  state.alertTab = tab;
  document.querySelectorAll('#alert-tabs .alert-tab').forEach(button => {
    button.classList.toggle('active', button.dataset.tab === tab);
  });
  const count = (state.alerts[tab] || []).length;
  $('alert-summary').textContent = tab === 'Playbooks'
    ? 'Review automated playbooks for churn prevention and growth.'
    : `Showing ${count} ${tab.toLowerCase()} alert${count === 1 ? '' : 's'} for prioritized customers.`;
  renderAlerts();
}

function renderAlerts() {
  const list = $('alerts-list');
  const playbooks = $('playbooks-list');
  list.setAttribute('aria-live', 'polite');
  list.setAttribute('role', 'feed');
  if (state.alertTab === 'Playbooks') {
    list.innerHTML = '<div class="empty-card">Switch back to an alert tab to manage incidents.</div>';
    playbooks.innerHTML = [`
      <div class="playbook-card">
        <div class="playbook-meta"><h4>Retention Booster</h4><span class="alert-badge Low">Active</span></div>
        <div class="playbook-steps">
          <div class="playbook-step">Send personalized renewal offer</div>
          <div class="playbook-step">Schedule CSM check-in</div>
          <div class="playbook-step">Review support tickets</div>
        </div>
        <div class="playbook-footer"><button class="btn-secondary">Run Manually</button><button class="btn-secondary">Edit</button></div>
      </div>
      <div class="playbook-card">
        <div class="playbook-meta"><h4>Engagement Surge</h4><span class="alert-badge Medium">Paused</span></div>
        <div class="playbook-steps">
          <div class="playbook-step">Launch feature adoption campaign</div>
          <div class="playbook-step">Invite to product webinar</div>
        </div>
        <div class="playbook-footer"><button class="btn-secondary">Run Manually</button><button class="btn-secondary">Edit</button></div>
      </div>
    `].join('');
    return;
  }
  const items = state.alerts[state.alertTab] || [];
  list.innerHTML = items.length ? items.map(alert => `
    <div class="alert-card">
      <div class="alert-meta">
        <div>
          <div class="alert-title">${alert.title}</div>
          <div class="alert-sub">Customer ${alert.customer} · ${alert.time}</div>
        </div>
        <span class="alert-badge ${alert.severity}">${alert.severity}</span>
      </div>
      <p>${alert.details}</p>
      <div class="alert-badges">${alert.signals.map(signal => `<span class="alert-badge ${alert.severity}">${signal}</span>`).join('')}</div>
      <div class="alert-actions"><button class="btn-secondary">Acknowledge</button><button class="btn-secondary">Assign CSM</button></div>
    </div>
  `).join('') : '<div class="empty-card">No alerts in this folder yet.</div>';
  playbooks.innerHTML = '<div class="empty-card">Select the Playbooks tab to review automation templates.</div>';
}

function addEventListeners() {
  $('drawer-close').addEventListener('click', closeCustomerDrawer);
  $('customer-drawer').addEventListener('click', e => {
    if (e.target.id === 'customer-drawer') closeCustomerDrawer();
  });
  $('drawer-train-btn').addEventListener('click', () => {
    closeCustomerDrawer();
    navigateTo('upload');
  });
}

addEventListeners();

function formatBytes(bytes) {
  if (bytes < 1024)        return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function animateValue(id, from, to, duration) {
  const el = $(id), start = performance.now(), range = to - from;
  function step(now) {
    const t = Math.min((now - start) / duration, 1);
    const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    el.textContent = Math.round(from + range * e);
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

function animateCounter(id, from, to, duration, fmt = v => Math.round(v)) {
  const el = $(id), start = performance.now(), range = to - from;
  function step(now) {
    const t = Math.min((now - start) / duration, 1);
    const e = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    el.textContent = fmt(from + range * e);
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ══════════════════  FIREBASE AUTH FLOW  ════════════════════ */
let activeAuthTab = 'login';

async function loadPersistedResults() {
  try {
    const res = await fetch(`${API}/results`);
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.summary) {
      state.results      = data;
      state.modelTrained = true;
      state.allCustomers = data.customers || [];
      updateModelStatus(data.summary.algorithm);
      populateDashboard(data);
      showToast('Restored a previous model session.', 'success');
    }
  } catch (err) {
    console.warn('Unable to load persisted model state:', err);
  }
}

function initAuthUI() {
  // Show overlay while checking auth state
  $('auth-overlay').style.display = 'flex';

  $('tab-login').addEventListener('click',    () => setAuthTab('login'));
  $('tab-register').addEventListener('click', () => setAuthTab('register'));
  $('auth-form').addEventListener('submit',   handleEmailAuth);
  $('google-login-btn').addEventListener('click', handleGoogleAuth);
  $('logout-btn').addEventListener('click',   handleSignOut);

  onAuthStateChanged(auth, async firebaseUser => {
    if (firebaseUser) {
      state.user    = firebaseUser;
      state.idToken = await firebaseUser.getIdToken();

      const initials = firebaseUser.displayName
        ? firebaseUser.displayName.split(' ').map(n => n[0]).join('').slice(0, 2)
        : (firebaseUser.email ? firebaseUser.email.slice(0, 2) : '?');

      $('user-avatar').textContent       = initials.toUpperCase();
      $('user-display-name').textContent = firebaseUser.displayName || 'Team Member';
      $('user-display-email').textContent = firebaseUser.email;
      $('user-profile-section').style.display = 'flex';

      $('auth-overlay').style.opacity = '0';
      setTimeout(() => { $('auth-overlay').style.display = 'none'; }, 500);

      showToast(`Welcome back, ${firebaseUser.displayName || firebaseUser.email}!`, 'success');
      await refreshAdminAccess();
      updateAdminNavigation();
      await loadPersistedResults();
    } else {
      state.user    = null;
      state.idToken = null;
      state.admin.isAdmin = false;
      state.admin.users = [];
      state.admin.settings = null;
      state.admin.modelStatus = null;
      $('user-profile-section').style.display = 'none';
      updateAdminNavigation();
      $('auth-overlay').style.display   = 'flex';
      $('auth-overlay').style.opacity   = '1';
      await loadPersistedResults();
    }
  });
}

function setAuthTab(tab) {
  activeAuthTab = tab;
  $('tab-login').classList.toggle('active',    tab === 'login');
  $('tab-register').classList.toggle('active', tab === 'register');
  if (tab === 'login') {
    $('auth-subtitle').textContent    = 'Sign in to access your Churn Prediction Dashboard';
    $('submit-btn-text').textContent  = 'Sign In';
  } else {
    $('auth-subtitle').textContent    = 'Create a secure account to deploy and use ChurnSight';
    $('submit-btn-text').textContent  = 'Create Account';
  }
}

async function handleEmailAuth(e) {
  e.preventDefault();
  const email    = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const btn      = $('auth-submit-btn');

  btn.disabled = true;
  const originalText = $('submit-btn-text').textContent;
  $('submit-btn-text').innerHTML =
    `<span class="spinner-inline"></span> ${activeAuthTab === 'login' ? 'Signing In...' : 'Registering...'}`;

  try {
    if (activeAuthTab === 'login') {
      await signInWithEmailAndPassword(auth, email, password);
    } else {
      await createUserWithEmailAndPassword(auth, email, password);
    }
  } catch (err) {
    showToast(err.message, 'error');
    btn.disabled = false;
    $('submit-btn-text').textContent = originalText;
  }
}

async function handleGoogleAuth() {
  const provider = new GoogleAuthProvider();
  try {
    await signInWithPopup(auth, provider);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function refreshAdminAccess() {
  if (!state.user) {
    state.admin.isAdmin = false;
    updateAdminNavigation();
    return;
  }

  try {
    const token = await state.user.getIdToken(true);
    const res = await fetch(`${API}/admin/whoami`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` }
    });
    const body = await res.json();
    state.admin.isAdmin = body.is_admin === true;
    if (state.admin.isAdmin && $('section-admin').classList.contains('active')) {
      await loadAdminPanel();
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
    await signOut(auth);
    showToast('Signed out successfully.', 'success');
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

/* ══════════════════  INIT  ════════════════════════════════ */
$('download-sample').href = `${location.origin}/sample_data.csv`;
initAuthUI();
navigateTo('upload');
