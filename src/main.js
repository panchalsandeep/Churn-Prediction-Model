/* ═══════════════════════════════════════════════════════════════
   ChurnSight  –  Frontend Application (Modular Firebase SDK)
═══════════════════════════════════════════════════════════════ */

import '../static/style.css';

import { loadChart, chartTheme } from './lib/charts.js';
import { loadFirebase } from './lib/firebase.js';
import { initTheme, onThemeChange, toggleTheme } from './lib/theme.js';
import { initPalette } from './lib/palette.js';
import { createWhatIf } from './lib/whatif.js';
import {
  animateNumber, tween, replay, stagger, debounce, prefersReducedMotion,
} from './lib/motion.js';

// Chart.js and Firebase are loaded on demand; these are filled in once ready.
let Chart = null;
let fb = null;

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
  pageSize          : Number(safeStorage('churnsight-page-size')) || 20,
  sort              : { key: null, dir: 'asc' },
  selected          : new Set(),
  chartsDirty       : new Set(),
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

function safeStorage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage may be blocked */ }
  return null;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.user) {
    // The SDK caches the token and refreshes it shortly before expiry, so no forced round-trip.
    state.idToken = await state.user.getIdToken();
  }
  if (state.idToken) headers.Authorization = `Bearer ${state.idToken}`;
  return headers;
}

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

let currentSection = 'upload';
let navLock = false;

/* Elements that fade/slide in (staggered) whenever their section opens. */
const REVEAL_SELECTOR = [
  '.reveal', '.card', '.kpi-card', '.insight-tile', '.upload-hero', '.health-header',
  '.cohort-toolbar', '.alerts-toolbar', '.customers-toolbar',
].join(',');

function revealSection(section) {
  const root = $(`section-${section}`);
  const nodes = Array.from(root.querySelectorAll(REVEAL_SELECTOR))
    .filter(node => !node.parentElement.closest(REVEAL_SELECTOR) || node.classList.contains('reveal'));
  nodes.forEach((node, i) => {
    node.style.setProperty('--delay', String(Math.min(i, 9)));
    replay(node, 'reveal');
  });
}

function moveNavIndicator() {
  const indicator = $('nav-indicator');
  const active = document.querySelector('.nav-item.active');
  if (!indicator || !active) return;
  indicator.style.transform = `translateY(${active.offsetTop}px)`;
  indicator.style.height = `${active.offsetHeight}px`;
  indicator.classList.add('is-ready');
}

function setPageTitle(text) {
  const el = $('page-title');
  el.textContent = text;
  replay(el, 'title-swap');
}

function navigateTo(section) {
  if (navLock) return;

  const apply = () => {
    sections.forEach(s => {
      const navItem = $(`nav-${s}`);
      const el = $(`section-${s}`);
      el.classList.remove('is-leaving');
      el.classList.toggle('active', s === section);
      navItem.classList.toggle('active', s === section);
      if (s === section) navItem.setAttribute('aria-current', 'page');
      else navItem.removeAttribute('aria-current');
    });
    currentSection = section;
    setPageTitle(titles[section]);
    moveNavIndicator();
    revealSection(section);
    renderSectionCharts(section);
    if (section === 'admin') loadAdminPanel();
    if (window.innerWidth <= 768) {
      document.querySelector('.sidebar').classList.remove('open');
    }
    window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    navLock = false;
  };

  const leaving = $(`section-${currentSection}`);
  if (section === currentSection || prefersReducedMotion() || !leaving?.classList.contains('active')) {
    apply();
    return;
  }

  navLock = true;
  leaving.classList.add('is-leaving');
  leaving.classList.remove('active');
  window.setTimeout(apply, 180);
}

/** Navigation with the same guards as clicking the sidebar. */
function requestNavigate(sec) {
  if (sec === 'admin' && !state.admin.isAdmin) {
    showToast('Admin access is restricted to administrators only.', 'error');
    return;
  }
  if (!state.modelTrained && sec !== 'upload' && sec !== 'predict' && sec !== 'admin') {
    showToast('Please upload data and train a model first.', 'error');
    return;
  }
  navigateTo(sec);
}

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    requestNavigate(el.dataset.section);
  });
});
window.addEventListener('resize', debounce(moveNavIndicator, 120));

function updateAdminNavigation() {
  const adminNav = $('nav-admin');
  if (!adminNav) return;
  adminNav.style.display = state.admin.isAdmin ? 'flex' : 'none';
  moveNavIndicator();
}


$('menu-toggle').addEventListener('click', () => {
  document.querySelector('.sidebar').classList.toggle('open');
});

/* ══════════════════  CLOCK  ═══════════════════════════════════ */
function updateClock() {
  if (document.hidden) return;
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
  replay($('file-info'), 'pop-in');
  $('drop-zone').style.display = 'none';
  $('train-btn').disabled = false;
}

$('remove-file').addEventListener('click', () => {
  state.selectedFile = null;
  fileInput.value = '';
  $('file-info').style.display = 'none';
  $('drop-zone').style.display = 'block';
  replay($('drop-zone'), 'pop-in');
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
    const headers = await authHeaders();
    const res  = await fetch(`${API}/upload`, { method: 'POST', headers, body: fd });
    const data = await res.json();

    if (!res.ok) {
      hideLoading();
      showToast(data.error || 'Training failed.', 'error');
      return;
    }

    showProgress(80, 'Building visualizations…');
    state.results      = data;
    state.modelTrained = true;
    state.allCustomers = data.customers || [];
    showProgress(100, 'Done!');
    await sleep(250);

    hideLoading();
    updateModelStatus(data.summary.algorithm);
    populateDashboard(data);
    const source = data.metrics_source === 'holdout_test_set' ? 'holdout test' : 'evaluation';
    showToast(`Model trained! ${source} accuracy: ${data.metrics.accuracy}%`, 'success');
    navigateTo('overview');

  } catch (err) {
    hideLoading();
    showToast(`Connection error: ${err.message}`, 'error');
  }
}

/* ══════════════════  DASHBOARD POPULATION  ════════════════════ */
function populateDashboard(data) {
  const { metrics, summary, confusion_matrix: cm } = data;

  const pct = v => `${(+v).toFixed(v % 1 ? 1 : 0)}%`;
  animateNumber($('kpi-total-val'), summary.total_customers, { duration: 900 });
  animateNumber($('kpi-high-val'), summary.high_risk, { duration: 900 });
  animateNumber($('kpi-churn-val'), summary.churn_rate, { duration: 900, format: v => `${v.toFixed(1)}%` });
  animateNumber($('kpi-acc-val'), metrics.accuracy, { duration: 1000, format: v => `${v.toFixed(1)}%` });
  animateNumber($('kpi-f1-val'), metrics.f1_score, { duration: 1000, format: v => `${v.toFixed(1)}%` });
  animateNumber($('kpi-auc-val'), metrics.auc_roc, { duration: 1000, format: v => `${v.toFixed(1)}%` });
  $('kpi-total').classList.remove('is-skeleton');

  const badge = $('algo-badge');
  badge.textContent   = summary.algorithm;
  badge.style.display = 'flex';
  replay(badge, 'pop-in');

  animateNumber($('cm-tn'), cm[0][0], { duration: 800 });
  animateNumber($('cm-fp'), cm[0][1], { duration: 800 });
  animateNumber($('cm-fn'), cm[1][0], { duration: 800 });
  animateNumber($('cm-tp'), cm[1][1], { duration: 800 });

  $('ring-acc-val').textContent  = pct(metrics.accuracy);
  $('ring-prec-val').textContent = pct(metrics.precision);
  $('ring-rec-val').textContent  = pct(metrics.recall);
  $('ring-f1-val').textContent   = pct(metrics.f1_score);

  // Charts are built the first time their section is shown (and rebuilt after new data / theme change).
  destroyAllCharts();
  state.chartsDirty = new Set(['overview', 'analytics', 'cohorts']);
  renderSectionCharts(currentSection);

  state.sort = { key: null, dir: 'asc' };
  state.selected.clear();
  state.currentPage = 1;
  customerIndex.clear();
  prepareCustomers();
  renderCustomerTable();
  renderHealthScores();
  buildCohortData();
  renderCohortView();
  initAlerts();
  populateInsights(data);
}

function populateInsights(data) {
  const customers = state.allCustomers || [];
  const high = customers.filter(c => c.risk_level === 'High');
  const revenue = high.reduce((sum, c) => sum + (Number(c.monthly) || 0), 0);
  const driver = data.feature_importance?.[0];
  const openAlerts = state.alerts?.Open?.length || 0;

  if ($('overview-lede')) {
    $('overview-lede').textContent = high.length
      ? `${high.length} accounts need outreach now. Holdout accuracy ${data.metrics?.accuracy ?? '–'}% · ${data.summary?.test_size ?? '–'} test rows.`
      : 'Portfolio looks stable. Keep monitoring tenure bands and support signals.';
  }
  if ($('insight-revenue')) {
    $('insight-revenue').textContent = revenue
      ? `$${Math.round(revenue).toLocaleString()}`
      : '$0';
    $('insight-revenue-meta').textContent = `${high.length} high-risk accounts × monthly charges`;
  }
  if ($('insight-driver')) {
    $('insight-driver').textContent = driver
      ? driver.feature.replace(/_/g, ' ')
      : '—';
    $('insight-driver-meta').textContent = driver
      ? `Importance ${driver.importance}`
      : 'Train a model to rank drivers';
  }
  if ($('insight-action')) {
    $('insight-action').textContent = openAlerts
      ? `Work ${openAlerts} open retention alerts`
      : high.length
        ? 'Prioritize high-risk CSM check-ins'
        : 'Keep standard engagement cadence';
    $('insight-action-meta').textContent = 'Derived from risk tiers + alert rules';
  }
}

function exportCustomersCsv(rows = state.allCustomers, label = '') {
  if (!rows.length) {
    showToast('Train a model before exporting.', 'error');
    return;
  }
  const headers = [
    'id', 'risk_level', 'churn_prob', 'tenure', 'monthly', 'support',
    'last_login', 'location', 'contract_type', 'payment_method', 'actual'
  ];
  const lines = [headers.join(',')];
  rows.forEach(c => {
    lines.push(headers.map(key => {
      const raw = c[key] ?? '';
      const value = String(raw).replace(/"/g, '""');
      return `"${value}"`;
    }).join(','));
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `churnsight-risk-export-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  showToast(`Exported ${rows.length} ${label}customers.`, 'success');
}

/* ══════════════════  CHART BUILDERS  ══════════════════════════ */
const CHART_SECTIONS = {
  overview : [() => buildTrendChart(state.results.trend_data), () => buildRiskDonut(state.results.summary)],
  analytics: [
    () => buildProbDistChart(state.results.prob_distribution),
    () => buildROCChart(state.results.roc_curve),
    () => buildFeatureChart(state.results.feature_importance),
    () => buildRingCharts(state.results.metrics),
  ],
  cohorts  : [() => buildCohortTrendChart(), () => buildContractChart()],
};

function destroyChart(key) {
  if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
}
function destroyAllCharts() {
  Object.keys(state.charts).forEach(destroyChart);
}

let animateCharts = true;
const chartAnimation = (duration = 900) => (animateCharts && !prefersReducedMotion() ? { duration, easing: 'easeOutQuart' } : false);

/** Build the charts for a section once, on first view (Chart.js itself loads lazily). */
async function renderSectionCharts(section, { animate = true } = {}) {
  if (!state.chartsDirty.has(section) || !state.results) return;
  state.chartsDirty.delete(section);
  try {
    Chart = await loadChart();
  } catch (err) {
    state.chartsDirty.add(section);
    showToast('Could not load the charting library. Check your connection.', 'error');
    return;
  }
  if (section === 'cohorts' && !state.cohorts) return;
  animateCharts = animate;
  CHART_SECTIONS[section].forEach(build => build());
}

function axisTitle(text) {
  return { display: true, text, color: chartTheme().title };
}

function buildTrendChart(td) {
  destroyChart('trend');
  const note = $('trend-note');
  if (note) note.textContent = td.note || 'Churn rate by tenure band on the uploaded dataset.';
  const ctx = $('trendChart').getContext('2d');
  const t = chartTheme();
  const red = t.dark ? '#E26B6B' : '#C23B3B';
  const teal = t.dark ? '#4DB8B3' : '#147A7A';
  state.charts.trend = new Chart(ctx, {
    type: 'line',
    data: {
      labels: td.months,
      datasets: [
        {
          label: 'Churn Rate %',
          data: td.churn_rate,
          borderColor: red,
          backgroundColor: 'rgba(194,59,59,0.10)',
          tension: 0.35, fill: true,
          pointBackgroundColor: red, pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5,
        },
        {
          label: 'Retention %',
          data: td.retained,
          borderColor: teal,
          backgroundColor: 'rgba(20,122,122,0.08)',
          tension: 0.35, fill: true,
          pointBackgroundColor: teal, pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: chartAnimation(1100),
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, pointStyleWidth: 10, padding: 18, color: t.text } },
        tooltip: tooltipStyle(),
      },
      scales: {
        x: { ...gridStyle(), title: axisTitle('Tenure band') },
        y: {
          ...gridStyle(),
          min: 0,
          max: 100,
          title: axisTitle('Rate (%)'),
          ticks: { ...gridStyle().ticks, callback: v => v + '%' },
        },
      },
    },
  });
}

function buildRiskDonut(summary) {
  destroyChart('donut');
  const ctx    = $('riskDonutChart').getContext('2d');
  const labels = ['High Risk', 'Medium Risk', 'Low Risk'];
  const data   = [summary.high_risk, summary.medium_risk, summary.low_risk];
  const dark   = chartTheme().dark;
  const colors = dark ? ['#E26B6B', '#F0993A', '#3FBF86'] : ['#C23B3B', '#D97706', '#1F8A5B'];

  state.charts.donut = new Chart(ctx, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: 'transparent', hoverOffset: 8, borderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '72%',
      animation: animateCharts && !prefersReducedMotion()
        ? { animateRotate: true, animateScale: true, duration: 1100, easing: 'easeOutQuart' } : false,
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
    },
  });

  const donutLegend = $('donut-legend');
  donutLegend.setAttribute('role', 'list');
  donutLegend.innerHTML = labels.map((l, i) => `
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
      animation: chartAnimation(900),
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: axisTitle('Churn Probability Range') },
        y: { ...gridStyle(), title: axisTitle('Number of Customers') },
      },
    },
  });
}

function buildROCChart(roc) {
  destroyChart('roc');
  const ctx = $('rocChart').getContext('2d');
  const t = chartTheme();
  state.charts.roc = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'ROC Curve',
          data: roc.fpr.map((x, i) => ({ x, y: roc.tpr[i] })),
          borderColor: t.primary, backgroundColor: t.dark ? 'rgba(127,209,207,0.10)' : 'rgba(11,61,74,0.08)',
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2.5,
        },
        {
          label: 'Random Classifier',
          data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
          borderColor: t.reference, borderDash: [6, 4],
          pointRadius: 0, borderWidth: 1.5, fill: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: chartAnimation(1200),
      plugins: { legend: { position: 'top', labels: { usePointStyle: true, padding: 14, color: t.text } }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), type: 'linear', min: 0, max: 1, title: axisTitle('False Positive Rate') },
        y: { ...gridStyle(), min: 0, max: 1, title: axisTitle('True Positive Rate') },
      },
    },
  });
}

function buildFeatureChart(fi) {
  destroyChart('feature');
  const ctx = $('featureChart').getContext('2d');
  const top = fi.slice(0, 10);
  const light = chartTheme().dark ? 58 : 36;
  state.charts.feature = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(f => f.feature.replace(/_/g, ' ')),
      datasets: [{
        label: 'Importance',
        data: top.map(f => f.importance),
        backgroundColor: top.map((_, i) => `hsla(${168 + i * 8}, 42%, ${light}%, 0.85)`),
        borderRadius: 6, borderSkipped: false,
      }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      animation: chartAnimation(900),
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: axisTitle('Importance Score') },
        y: gridStyle(),
      },
    },
  });
}

function buildRingCharts(metrics) {
  const t = chartTheme();
  const rings = [
    { canvas: 'ringAccCanvas',  value: metrics.accuracy,  color: t.dark ? '#3FBF86' : '#1F8A5B' },
    { canvas: 'ringPrecCanvas', value: metrics.precision, color: t.dark ? '#6B9BFF' : '#2F6FED' },
    { canvas: 'ringRecCanvas',  value: metrics.recall,    color: t.dark ? '#F0993A' : '#D97706' },
    { canvas: 'ringF1Canvas',   value: metrics.f1_score,  color: t.dark ? '#4DB8B3' : '#147A7A' },
  ];
  rings.forEach(({ canvas, value, color }, i) => {
    destroyChart(canvas);
    const ctx = $(canvas).getContext('2d');
    state.charts[canvas] = new Chart(ctx, {
      type: 'doughnut',
      data: { datasets: [{ data: [value, 100 - value], backgroundColor: [color, t.track], borderWidth: 0, hoverOffset: 0 }] },
      options: {
        responsive: false, cutout: '78%',
        animation: animateCharts && !prefersReducedMotion()
          ? { animateRotate: true, duration: 1000, delay: i * 120, easing: 'easeOutQuart' } : false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
      },
    });
  });
}

/* Rebuild whatever is on screen when the theme flips; other sections rebuild on next visit. */
onThemeChange(() => {
  if (!state.results) return;
  destroyAllCharts();
  state.chartsDirty = new Set(['overview', 'analytics', 'cohorts']);
  renderSectionCharts(currentSection, { animate: false });
  if (gaugeState.probability != null) drawGauge('gaugeCanvas', gaugeState.probability);
});

/* ══════════════════  GAUGE CHART  ════════════════════════════ */
const gaugeState = { probability: null, cancel: null };

function drawGauge(canvas, probability) {
  const el = $(canvas);
  const ctx = el.getContext('2d');
  const t = chartTheme();
  // Crisp on high-DPI screens: size the backing store once, then draw in CSS pixels.
  const W = 220, H = 130, cx = W / 2, cy = H - 18, r = 90;
  const dpr = window.devicePixelRatio || 1;
  if (el.width !== W * dpr) {
    el.width = W * dpr; el.height = H * dpr;
    el.style.width = `${W}px`; el.style.height = `${H}px`;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0, false);
  ctx.strokeStyle = t.track;
  ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();

  const pct  = Math.max(0, Math.min(1, probability / 100));
  const end  = Math.PI + pct * Math.PI;
  const grad = ctx.createLinearGradient(cx - r, cy, cx + r, cy);
  if (probability >= 70) {
    grad.addColorStop(0, '#E8A838'); grad.addColorStop(1, '#C23B3B');
  } else if (probability >= 40) {
    grad.addColorStop(0, '#1F8A5B'); grad.addColorStop(1, '#E8A838');
  } else {
    grad.addColorStop(0, '#2F6FED'); grad.addColorStop(1, '#1F8A5B');
  }

  if (pct > 0) {
    ctx.beginPath();
    ctx.arc(cx, cy, r, Math.PI, end, false);
    ctx.strokeStyle = grad; ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();
  }

  // Marker at the end of the arc (a needle would cross the percentage label).
  const mx = cx + r * Math.cos(end);
  const my = cy + r * Math.sin(end);
  ctx.beginPath(); ctx.arc(mx, my, 10, 0, Math.PI * 2);
  ctx.fillStyle = t.dark ? '#142127' : '#FAF7F0'; ctx.fill();
  ctx.lineWidth = 3; ctx.strokeStyle = t.tooltipTitle; ctx.stroke();

  ctx.font = '500 10px "IBM Plex Sans"'; ctx.fillStyle = t.title; ctx.textAlign = 'center';
  ctx.fillText('0%',   cx - r + 4, cy + 18);
  ctx.fillText('50%',  cx,          cy - r - 8);
  ctx.fillText('100%', cx + r - 4, cy + 18);
}

/** Sweep the needle from its current position to the new probability. */
function animateGauge(probability) {
  gaugeState.cancel?.();
  const from = gaugeState.probability ?? 0;
  gaugeState.probability = probability;
  const label = $('gauge-val');
  gaugeState.cancel = tween(from === 0 ? 1100 : 600, t => {
    const value = from + (probability - from) * t;
    drawGauge('gaugeCanvas', value);
    label.textContent = value.toFixed(1) + '%';
  });
}

/* ══════════════════  CUSTOMER TABLE  ════════════════════════ */
const RISK_RANK = { High: 3, Medium: 2, Low: 1 };

/** Precompute search text and sort ranks once per dataset instead of on every keystroke. */
function prepareCustomers() {
  state.allCustomers.forEach(c => {
    c._q = [c.id, c.location, c.contract_type, c.payment_method].filter(Boolean).join(' ').toLowerCase();
    c._rank = RISK_RANK[c.risk_level] || 0;
  });
}

function sortValue(c, key) {
  if (key === 'id') return String(c.id).toLowerCase();
  if (key === 'risk_level') return c._rank;
  return Number(c[key]) || 0;
}

function getVisibleRows() {
  let rows = state.allCustomers;
  if (state.riskFilter !== 'All') rows = rows.filter(c => c.risk_level === state.riskFilter);
  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    rows = rows.filter(c => c._q.includes(q));
  }
  const { key, dir } = state.sort;
  if (key) {
    const sign = dir === 'asc' ? 1 : -1;
    rows = [...rows].sort((a, b) => {
      const av = sortValue(a, key), bv = sortValue(b, key);
      return av < bv ? -sign : av > bv ? sign : 0;
    });
  }
  return rows;
}

const SKELETON_ROW = `<tr class="skeleton-row" aria-hidden="true">${
  '<td><span class="skeleton"></span></td>'.repeat(9)}</tr>`;

function showTableSkeleton(rows = 8) {
  $('customer-tbody').innerHTML = SKELETON_ROW.repeat(rows);
  document.querySelectorAll('.kpi-card .kpi-value').forEach(el => el.classList.add('is-skeleton'));
}
function clearSkeleton() {
  document.querySelectorAll('.kpi-card .kpi-value.is-skeleton').forEach(el => el.classList.remove('is-skeleton'));
}

function renderCustomerTable({ animate = true } = {}) {
  clearSkeleton();
  const data = getVisibleRows();
  state.filteredCustomers = data;
  const total = data.length;
  const pages = Math.max(1, Math.ceil(total / state.pageSize));
  state.currentPage = Math.min(state.currentPage, pages);
  const start = (state.currentPage - 1) * state.pageSize;
  const paged = data.slice(start, start + state.pageSize);

  $('customer-count').textContent = total
    ? `Showing ${start + 1}–${start + paged.length} of ${total} customers`
    : 'Showing 0 customers';

  const tbody = $('customer-tbody');
  if (!paged.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-row">${
      state.allCustomers.length ? 'No customers match the current filters.' : 'No data loaded. Please train a model first.'
    }</td></tr>`;
  } else {
    tbody.innerHTML = paged.map(c => {
      const fillClass = c.risk_level === 'High' ? 'fill-high' : c.risk_level === 'Medium' ? 'fill-medium' : 'fill-low';
      const id = escapeHtml(c.id);
      const risk = escapeHtml(c.risk_level);
      const prob = Number(c.churn_prob) || 0;
      const checked = state.selected.has(c.id);
      return `
        <tr class="customer-row${checked ? ' is-selected' : ''}" data-customer-id="${id}" tabindex="0" aria-selected="${checked}">
          <td class="col-select"><input type="checkbox" class="row-check" aria-label="Select ${id}" ${checked ? 'checked' : ''} /></td>
          <td><strong>${id}</strong></td>
          <td><span class="risk-tag ${risk}">${risk}</span></td>
          <td>
            <div class="prob-bar-wrap">
              <div class="prob-mini-bar">
                <div class="prob-mini-fill ${fillClass}" style="width:${prob}%"></div>
              </div>
              <span>${prob}%</span>
            </div>
          </td>
          <td>${Number(c.tenure) || 0} mo</td>
          <td>$${(Number(c.monthly) || 0).toFixed(2)}</td>
          <td>${Number(c.support) || 0}</td>
          <td>${Number(c.last_login) || 0} days</td>
          <td>${c.actual === 1
            ? '<span class="churn-yes">● Churned</span>'
            : '<span class="churn-no">● Retained</span>'
          }</td>
        </tr>`;
    }).join('');
    if (animate) {
      const rows = tbody.querySelectorAll('.customer-row');
      stagger(rows, 16);
      rows.forEach(row => row.classList.add('row-in'));
    }
  }
  renderPagination(pages);
  syncSortHeaders();
  syncSelectionUi();
}

function renderPagination(pages) {
  const wrap = $('table-pagination');
  if (pages <= 1) { wrap.innerHTML = ''; return; }
  const cur = state.currentPage;

  // 1 … cur-1 cur cur+1 … last
  const shown = new Set([1, pages, cur - 1, cur, cur + 1].filter(n => n >= 1 && n <= pages));
  let html = `<button class="page-btn" ${cur === 1 ? 'disabled' : ''} data-page="${cur - 1}">‹ Prev</button>`;
  let prev = 0;
  [...shown].sort((a, b) => a - b).forEach(n => {
    if (n - prev > 1) html += '<span class="page-gap" aria-hidden="true">…</span>';
    html += `<button class="page-btn ${n === cur ? 'active' : ''}" data-page="${n}" ${n === cur ? 'aria-current="page"' : ''}>${n}</button>`;
    prev = n;
  });
  html += `<button class="page-btn" ${cur === pages ? 'disabled' : ''} data-page="${cur + 1}">Next ›</button>`;
  wrap.innerHTML = html;
}

$('table-pagination').addEventListener('click', e => {
  const btn = e.target.closest('.page-btn[data-page]');
  if (!btn || btn.disabled) return;
  state.currentPage = parseInt(btn.dataset.page, 10);
  renderCustomerTable();
  $('customer-table').scrollIntoView({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
});

/* Sorting */
function syncSortHeaders() {
  document.querySelectorAll('.sort-btn').forEach(btn => {
    const active = state.sort.key === btn.dataset.sort;
    btn.dataset.dir = active ? state.sort.dir : '';
    btn.closest('th').setAttribute('aria-sort', active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
}

document.querySelector('#customer-table thead').addEventListener('click', e => {
  const btn = e.target.closest('.sort-btn');
  if (!btn) return;
  const key = btn.dataset.sort;
  // asc -> desc -> unsorted
  if (state.sort.key !== key) state.sort = { key, dir: 'asc' };
  else if (state.sort.dir === 'asc') state.sort = { key, dir: 'desc' };
  else state.sort = { key: null, dir: 'asc' };
  state.currentPage = 1;
  renderCustomerTable();
});

/* Selection + bulk actions */
function syncSelectionUi() {
  const bar = $('bulk-bar');
  const count = state.selected.size;
  $('bulk-count').textContent = `${count} selected`;
  if (count && bar.hidden) { bar.hidden = false; replay(bar, 'pop-in'); }
  else if (!count) bar.hidden = true;

  const checks = [...document.querySelectorAll('#customer-tbody .row-check')];
  const selectedOnPage = checks.filter(c => c.checked).length;
  const all = $('select-all');
  all.checked = checks.length > 0 && selectedOnPage === checks.length;
  all.indeterminate = selectedOnPage > 0 && selectedOnPage < checks.length;
}

$('customer-tbody').addEventListener('change', e => {
  const check = e.target.closest('.row-check');
  if (!check) return;
  const row = check.closest('.customer-row');
  const id = row.dataset.customerId;
  if (check.checked) state.selected.add(id); else state.selected.delete(id);
  row.classList.toggle('is-selected', check.checked);
  row.setAttribute('aria-selected', String(check.checked));
  syncSelectionUi();
});

$('select-all').addEventListener('change', e => {
  document.querySelectorAll('#customer-tbody .customer-row').forEach(row => {
    const on = e.target.checked;
    if (on) state.selected.add(row.dataset.customerId); else state.selected.delete(row.dataset.customerId);
    row.classList.toggle('is-selected', on);
    row.setAttribute('aria-selected', String(on));
    row.querySelector('.row-check').checked = on;
  });
  syncSelectionUi();
});

$('bulk-clear').addEventListener('click', () => { state.selected.clear(); renderCustomerTable({ animate: false }); });
$('bulk-export').addEventListener('click', () => {
  exportCustomersCsv(state.allCustomers.filter(c => state.selected.has(c.id)), 'selected ');
});

/* Opening the drawer: delegated, and keyboard accessible */
$('customer-tbody').addEventListener('click', e => {
  if (e.target.closest('.col-select')) return;
  const row = e.target.closest('.customer-row');
  if (row) showCustomerDrawer(row.dataset.customerId, row);
});
$('customer-tbody').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  if (e.target.closest('.col-select')) return;
  const row = e.target.closest('.customer-row');
  if (row) { e.preventDefault(); showCustomerDrawer(row.dataset.customerId, row); }
});

/* Filters, search, page size */
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

const applySearch = debounce(value => {
  state.searchQuery = value;
  state.currentPage = 1;
  renderCustomerTable();
}, 160);
$('customer-search').addEventListener('input', e => applySearch(e.target.value.trim()));

$('page-size').value = String(state.pageSize);
$('page-size').addEventListener('change', e => {
  state.pageSize = Number(e.target.value) || 20;
  safeStorage('churnsight-page-size', String(state.pageSize));
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
const PREDICT_BTN_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg> Predict Churn';

function readPredictForm() {
  return {
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
}

async function requestPrediction(payload, { explain = false, signal } = {}) {
  const headers = await authHeaders({ 'Content-Type': 'application/json' });
  const res = await fetch(`${API}/predict`, {
    method: 'POST', headers, signal, body: JSON.stringify({ ...payload, explain }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Prediction failed.');
  return data;
}

const whatIf = createWhatIf({
  score: (payload, { signal }) => requestPrediction(payload, { explain: true, signal }),
  render: data => renderPrediction(data),
});

$('predict-form').addEventListener('submit', async e => {
  e.preventDefault();

  if (!state.modelTrained) {
    showToast('Please train a model first.', 'error');
    return;
  }

  const btn = $('predict-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span> Predicting…';

  const payload = readPredictForm();
  try {
    const data = await requestPrediction(payload, { explain: true });
    renderPrediction(data);
    whatIf.start(payload, data);
  } catch (err) {
    showToast(err.message || 'Prediction failed.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = PREDICT_BTN_HTML;
  }
});

let lastRecs = '';
let lastRisk = '';

function renderPrediction(data) {
  const { churn_probability: prob, risk_level: risk, recommendation: recs = [] } = data;

  const content = $('result-content');
  if (content.style.display === 'none') {
    $('result-placeholder').style.display = 'none';
    content.style.display = 'block';
    replay(content, 'pop-in');
  }

  animateGauge(prob);

  if (risk !== lastRisk) {
    lastRisk = risk;
    const badge = $('risk-badge-large');
    badge.className = `risk-badge-large ${risk}`;
    $('risk-label-large').textContent = risk + ' Risk';
    replay(badge, 'badge-pop');
  }

  const key = recs.join('|');
  if (key !== lastRecs) {
    lastRecs = key;
    const list = $('recs-list');
    list.innerHTML = recs.map(r => `<li>${escapeHtml(r)}</li>`).join('');
    stagger(list.children, 8);
  }
}

/* ══════════════════  HELPERS  ══════════════════════════════ */
function tooltipStyle() {
  const t = chartTheme();
  return {
    backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1,
    titleColor: t.tooltipTitle, bodyColor: t.text,
    titleFont: { weight: '700' }, bodyFont: { size: 12 }, padding: 10, cornerRadius: 8,
  };
}

function gridStyle() {
  const t = chartTheme();
  return {
    grid  : { color: t.grid },
    ticks : { color: t.text, font: { size: 11 } },
    border: { color: 'transparent' },
  };
}

function setOverlay(el, open) {
  el.classList.toggle('is-open', open);
  el.setAttribute('aria-hidden', String(!open));
}

function showLoading(text = 'Processing…') {
  $('loading-text').textContent = text;
  setOverlay($('loading-overlay'), true);
}
function hideLoading() {
  setOverlay($('loading-overlay'), false);
  $('progress-wrap').style.display   = 'none';
}
function showProgress(pct, label) {
  $('progress-wrap').style.display  = 'block';
  $('progress-fill').style.width    = pct + '%';
  $('progress-label').textContent   = label;
}

let toastTimer = 0;
function showToast(msg, type = 'success') {
  const t = $('toast');
  t.textContent = msg;
  t.className   = `toast ${type} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}
function updateModelStatus(algo) {
  $('model-status-badge').innerHTML =
    `<span class="status-dot active"></span><span>${escapeHtml(algo)} Ready</span>`;
}

const customerIndex = new Map();
function getCustomerById(id) {
  if (customerIndex.size !== state.allCustomers.length) {
    customerIndex.clear();
    state.allCustomers.forEach(c => customerIndex.set(c.id, c));
  }
  return customerIndex.get(id);
}

let drawerReturnFocus = null;

function showCustomerDrawer(id, trigger) {
  const customer = getCustomerById(id);
  if (!customer) return;
  const drawer = $('customer-drawer');
  $('drawer-customer-id').textContent = customer.id;
  $('drawer-risk-tag').textContent = customer.risk_level;
  $('drawer-risk-tag').className = `risk-tag ${customer.risk_level}`;
  animateNumber($('drawer-churn-prob'), Number(customer.churn_prob) || 0, { duration: 700, format: v => `${v.toFixed(1)}%` });
  $('drawer-prob-fill').style.setProperty('--w', `${Math.min(100, Number(customer.churn_prob) || 0)}%`);
  $('drawer-prob-fill').className = `drawer-prob-fill ${customer.risk_level}`;
  $('drawer-tenure').textContent = `${customer.tenure} mo`;
  $('drawer-monthly').textContent = `$${(Number(customer.monthly) || 0).toFixed(2)}`;
  $('drawer-support').textContent = customer.support;
  $('drawer-last-login').textContent = `${customer.last_login} days`;
  $('drawer-contract').textContent = customer.contract_type || 'N/A';
  $('drawer-payment').textContent = customer.payment_method || 'N/A';
  $('drawer-action').textContent = customer.risk_level === 'High'
    ? 'Prioritize outreach and service recovery for this customer.'
    : 'Continue normal customer success playbook.';
  stagger(drawer.querySelectorAll('.drawer-row'), 10);
  drawer.dataset.customerId = customer.id;

  drawerReturnFocus = trigger || document.activeElement;
  setOverlay(drawer, true);
  $('drawer-close').focus({ preventScroll: true });
}

function closeCustomerDrawer() {
  const drawer = $('customer-drawer');
  if (!drawer.classList.contains('is-open')) return;
  setOverlay(drawer, false);
  if (drawerReturnFocus && document.contains(drawerReturnFocus)) drawerReturnFocus.focus({ preventScroll: true });
  drawerReturnFocus = null;
}

/** Load a customer's known attributes into the predict form and score them. */
function simulateCustomer(id) {
  const c = getCustomerById(id);
  if (!c) return;
  const set = (field, value) => { if (value !== undefined && value !== null && value !== '') $(field).value = value; };
  set('p-tenure', c.tenure);
  set('p-monthly', c.monthly);
  set('p-total', +((Number(c.tenure) || 0) * (Number(c.monthly) || 0)).toFixed(2));
  set('p-tickets', c.support);
  set('p-lastlogin', c.last_login);
  set('p-location', c.location);
  set('p-contract', c.contract_type);
  set('p-payment', c.payment_method);
  closeCustomerDrawer();
  navigateTo('predict');
  $('predict-form').requestSubmit();
  showToast(`Loaded ${c.id}. Age, products and logins use form defaults.`, 'success');
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
  // Scores depend only on the customer row, so compute them once per dataset.
  state.allCustomers.forEach(c => { c._health ??= calculateHealthScore(c); });
  const list = state.allCustomers.map(c => ({ ...c, ...c._health }));
  const filtered = state.healthFilter === 'All' ? list : list.filter(c => c.risk_level === state.healthFilter);
  const top = [...filtered].sort((a,b) => b.score - a.score).slice(0, 9);
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

  container.innerHTML = top.map((customer, idx) => {
    const tier = customer.score >= 75 ? 'Low' : customer.score >= 45 ? 'Medium' : 'High';
    const worst = [...customer.components].sort((a,b) => a.value - b.value).slice(0, 2);
    return `
      <div class="health-card card-in" style="--i:${idx}">
        <div class="health-card-header">
          <h4>${escapeHtml(customer.id)}</h4>
          <span class="health-tier ${tier}">${tier} Risk</span>
        </div>
        <div class="health-score">${customer.score}</div>
        <div class="health-note">${customer.score < 45 ? 'Urgent action required to retain this account.' : 'Monitor key signals and engage proactively.'}</div>
        ${customer.components.map(comp => `
          <div class="health-component">
            <span>${escapeHtml(comp.name)}</span>
            <span>${comp.value}%</span>
          </div>
          <div class="health-bar"><div class="health-bar-fill" style="width:${comp.value}%"></div></div>
        `).join('')}
        <div class="health-note"><strong>Drag factor:</strong> ${escapeHtml(worst.map(c => c.name).join(' & '))}</div>
      </div>`;
  }).join('');
  container.querySelectorAll('.health-score').forEach((el, i) => {
    animateNumber(el, top[i].score, { duration: 800 });
  });
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
      const avgChurn = customers.length
        ? customers.reduce((sum, c) => sum + Number(c.churn_prob || 0), 0) / customers.length
        : 0;
      const avgTenure = customers.length
        ? customers.reduce((sum, c) => sum + Number(c.tenure || 0), 0) / customers.length
        : 0;
      // Deterministic pseudo-retention curve from churn risk + tenure (demo proxy, not calendar retention).
      const base = Math.max(25, Math.min(95, 92 - avgChurn * 0.45 + Math.min(avgTenure, 36) * 0.15));
      return months.map((_, index) => Math.max(15, Math.min(100, Math.round(base - index * (6 + avgChurn * 0.04)))));
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
  const cells = [];
  labels.forEach((label, rowIndex) => {
    months.forEach((month, monthIndex) => {
      const value = heatmap[rowIndex][monthIndex];
      const level = value >= 85 ? 5 : value >= 70 ? 4 : value >= 55 ? 3 : value >= 40 ? 2 : 1;
      // Diagonal wave so the grid ripples in from the top-left.
      const wave = Math.min(rowIndex + monthIndex, 14);
      cells.push(`<div class="heatmap-cell heat-${level} cell-in" style="--i:${wave}">${escapeHtml(label)} ${escapeHtml(month)}<br/><strong>${value}%</strong></div>`);
    });
  });
  container.innerHTML = cells.join('');

  const labelType = state.cohortType === 'contract' ? 'Contract Type' : state.cohortType === 'location' ? 'Location' : 'Signup Cohort';
  $('cohort-summary').textContent = `Displaying ${state.cohorts.totalCohorts} ${labelType} cohorts (proxy retention from risk/tenure — not a true calendar cohort).`;

  // Cohort charts depend on the selected cohort type: rebuild them when next needed.
  destroyChart('cohortTrend');
  destroyChart('contract');
  state.chartsDirty.add('cohorts');
  if (currentSection === 'cohorts') renderSectionCharts('cohorts');
}

function buildCohortTrendChart() {
  destroyChart('cohortTrend');
  const ctx = $('cohortTrendChart').getContext('2d');
  const t = chartTheme();
  const light = t.dark ? 58 : 34;
  state.charts.cohortTrend = new Chart(ctx, {
    type: 'line',
    data: {
      labels: state.cohorts.months,
      datasets: state.cohorts.labels.map((label, index) => ({
        label,
        data: state.cohorts.heatmap[index],
        borderColor: `hsl(${168 + index * 18}, 38%, ${light + index * 4}%)`, fill: false, tension: 0.3, pointRadius: 3, borderWidth: 2.5,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: chartAnimation(1000),
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'top', labels: { color: t.text } }, tooltip: tooltipStyle() },
      scales: {
        x: { ...gridStyle(), title: axisTitle('Month') },
        y: { ...gridStyle(), min: 0, max: 100, title: axisTitle('Retention %') },
      },
    },
  });
}

function buildContractChart() {
  destroyChart('contract');
  const ctx = $('contractChart').getContext('2d');
  const labels = Object.keys(state.cohorts.contractCounts);
  const values = Object.values(state.cohorts.contractCounts);
  const light = chartTheme().dark ? 56 : 38;
  state.charts.contract = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Customers', data: values,
        backgroundColor: labels.map((_, index) => `hsla(${168 + index * 16}, 40%, ${light}%, 0.85)`),
        borderRadius: 6, borderSkipped: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: chartAnimation(900),
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
  const rules = state.admin.settings?.alert_rules || [
    { id: 'reengagement', enabled: true, last_login_days: 21 },
    { id: 'support_escalation', enabled: true, support_tickets: 4 },
  ];
  const byId = Object.fromEntries(rules.map(r => [r.id, r]));
  const loginThreshold = Number(byId.reengagement?.last_login_days ?? 21);
  const supportThreshold = Number(byId.support_escalation?.support_tickets ?? 4);

  const open = [];
  state.allCustomers.forEach((customer, idx) => {
    const signals = [];
    if (byId.reengagement?.enabled !== false && Number(customer.last_login) > loginThreshold) {
      signals.push(`Inactive ${customer.last_login}d`);
    }
    if (byId.support_escalation?.enabled !== false && Number(customer.support) > supportThreshold) {
      signals.push(`Support tickets ${customer.support}`);
    }
    if (customer.risk_level === 'High') signals.push(`Risk ${customer.churn_prob}%`);
    if (!signals.length) return;
    if (open.length >= 6) return;
    open.push({
      id: `alert-open-${idx}`,
      customer: customer.id,
      severity: customer.risk_level,
      title: customer.risk_level === 'High' ? 'High churn risk detected' : 'Retention signal triggered',
      signals,
      time: `${2 + (idx % 8)}h ago`,
      status: 'Open',
      details: `Customer ${customer.id}: ${signals.join(' · ')}.`,
    });
  });

  const acknowledged = state.allCustomers
    .filter(c => c.risk_level === 'Medium')
    .slice(0, 2)
    .map((customer, idx) => ({
      id: `alert-ack-${idx}`,
      customer: customer.id,
      severity: customer.risk_level,
      title: 'Engagement declined',
      signals: ['CSM follow-up scheduled'],
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
  list.innerHTML = items.length ? items.map((alert, idx) => `
    <div class="alert-card card-in" style="--i:${Math.min(idx, 10)}" data-alert-id="${escapeHtml(alert.id)}">
      <div class="alert-meta">
        <div>
          <div class="alert-title">${escapeHtml(alert.title)}</div>
          <div class="alert-sub">Customer ${escapeHtml(alert.customer)} · ${escapeHtml(alert.time)}</div>
        </div>
        <span class="alert-badge ${escapeHtml(alert.severity)}">${escapeHtml(alert.severity)}</span>
      </div>
      <p>${escapeHtml(alert.details)}</p>
      <div class="alert-badges">${alert.signals.map(signal => `<span class="alert-badge ${escapeHtml(alert.severity)}">${escapeHtml(signal)}</span>`).join('')}
        ${alert.assignee ? `<span class="alert-badge assignee">Assigned: ${escapeHtml(alert.assignee)}</span>` : ''}</div>
      <div class="alert-actions">
        ${alert.status === 'Open' ? '<button class="btn-secondary" data-action="acknowledge" type="button">Acknowledge</button>' : ''}
        ${alert.status === 'Acknowledged' ? '<button class="btn-secondary" data-action="resolve" type="button">Mark resolved</button>' : ''}
        ${alert.status !== 'Resolved' && !alert.assignee ? '<button class="btn-secondary" data-action="assign" type="button">Assign to me</button>' : ''}
      </div>
    </div>
  `).join('') : '<div class="empty-card">No alerts in this folder yet.</div>';
  playbooks.innerHTML = '<div class="empty-card">Select the Playbooks tab to review automation templates.</div>';
}

/* Alert workflow: Open -> Acknowledged -> Resolved, with the card animating out of its list. */
const ALERT_NEXT = { acknowledge: 'Acknowledged', resolve: 'Resolved' };

$('alerts-list').addEventListener('click', e => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const card = btn.closest('.alert-card');
  const alert = (state.alerts[state.alertTab] || []).find(a => a.id === card.dataset.alertId);
  if (!alert) return;

  if (btn.dataset.action === 'assign') {
    alert.assignee = state.user?.displayName || state.user?.email || 'You';
    renderAlerts();
    showToast(`Assigned ${alert.customer} to ${alert.assignee}.`, 'success');
    return;
  }

  const target = ALERT_NEXT[btn.dataset.action];
  if (!target) return;
  const move = () => {
    state.alerts[state.alertTab] = state.alerts[state.alertTab].filter(a => a !== alert);
    alert.status = target;
    state.alerts[target].unshift(alert);
    setAlertTab(state.alertTab);
    populateInsights(state.results);
    showToast(`Alert for ${alert.customer} moved to ${target}.`, 'success');
  };
  if (prefersReducedMotion()) { move(); return; }
  card.classList.add('card-out');
  card.addEventListener('animationend', move, { once: true });
});

function addEventListeners() {
  $('drawer-close').addEventListener('click', closeCustomerDrawer);
  $('drawer-sim-btn').addEventListener('click', () => simulateCustomer($('customer-drawer').dataset.customerId));
  $('customer-drawer').addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeCustomerDrawer(); return; }
    if (e.key !== 'Tab') return;
    const focusable = [...$('customer-drawer').querySelectorAll('button:not([disabled])')];
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
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

/* ══════════════════  FIREBASE AUTH FLOW  ════════════════════ */
let activeAuthTab = 'login';

async function loadPersistedResults() {
  if (!state.modelTrained) showTableSkeleton();
  try {
    const headers = await authHeaders();
    const res = await fetch(`${API}/results`, { headers });
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
  } finally {
    if (!state.modelTrained) renderCustomerTable({ animate: false }); // drops the skeleton rows
  }
}

function showAuthOverlay() {
  const overlay = $('auth-overlay');
  overlay.style.display = 'flex';
  overlay.style.opacity = '1';
  overlay.setAttribute('aria-hidden', 'false');
  // Restart entrance animations
  overlay.classList.remove('is-visible');
  void overlay.offsetWidth;
  requestAnimationFrame(() => overlay.classList.add('is-visible'));
}

function hideAuthOverlay() {
  const overlay = $('auth-overlay');
  overlay.classList.remove('is-visible');
  overlay.classList.add('is-leaving');
  overlay.style.opacity = '0';
  overlay.setAttribute('aria-hidden', 'true');
  setTimeout(() => {
    overlay.style.display = 'none';
    overlay.classList.remove('is-leaving');
  }, 420);
}

async function initAuthUI() {
  showAuthOverlay();

  $('tab-login').addEventListener('click',    () => setAuthTab('login'));
  $('tab-register').addEventListener('click', () => setAuthTab('register'));
  $('auth-form').addEventListener('submit',   handleEmailAuth);
  $('google-login-btn').addEventListener('click', handleGoogleAuth);
  $('logout-btn').addEventListener('click',   handleSignOut);

  // The auth card is already interactive; the SDK streams in behind it.
  try {
    fb = await loadFirebase();
  } catch (err) {
    showToast('Could not load sign-in. Check your connection and reload.', 'error');
    return;
  }

  fb.onAuthStateChanged(fb.auth, async firebaseUser => {
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

      hideAuthOverlay();

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
      showAuthOverlay();
      await loadPersistedResults();
    }
  });
}

function setAuthTab(tab) {
  activeAuthTab = tab;
  $('tab-login').classList.toggle('active',    tab === 'login');
  $('tab-register').classList.toggle('active', tab === 'register');
  if (tab === 'login') {
    $('auth-subtitle').textContent    = 'Sign in to access your churn prediction workspace';
    $('submit-btn-text').textContent  = 'Sign In';
    qs('.auth-title').textContent     = 'Welcome back';
  } else {
    $('auth-subtitle').textContent    = 'Create an account to train models and run retention playbooks';
    $('submit-btn-text').textContent  = 'Create Account';
    qs('.auth-title').textContent     = 'Join ChurnSight';
  }
}

async function handleEmailAuth(e) {
  e.preventDefault();
  if (!fb) { showToast('Sign-in is still loading. Try again in a moment.', 'error'); return; }
  const email    = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const btn      = $('auth-submit-btn');

  btn.disabled = true;
  const originalText = $('submit-btn-text').textContent;
  $('submit-btn-text').innerHTML =
    `<span class="spinner-inline"></span> ${activeAuthTab === 'login' ? 'Signing In...' : 'Registering...'}`;

  try {
    if (activeAuthTab === 'login') {
      await fb.signInWithEmailAndPassword(fb.auth, email, password);
    } else {
      await fb.createUserWithEmailAndPassword(fb.auth, email, password);
    }
  } catch (err) {
    showToast(err.message, 'error');
    btn.disabled = false;
    $('submit-btn-text').textContent = originalText;
  }
}

async function handleGoogleAuth() {
  if (!fb) { showToast('Sign-in is still loading. Try again in a moment.', 'error'); return; }
  const provider = new fb.GoogleAuthProvider();
  try {
    await fb.signInWithPopup(fb.auth, provider);
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
    const headers = await authHeaders();
    const res = await fetch(`${API}/admin/whoami`, { method: 'GET', headers });
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
    const headers = await authHeaders();
    const res = await fetch(`${API}/admin/users`, { headers });
    const body = await res.json();
    if (!res.ok) {
      list.innerHTML = `<tr><td class="empty-row" colspan="5">${escapeHtml(body.error || 'Unable to fetch users')}</td></tr>`;
      return;
    }
    state.admin.users = body.users || [];
    renderAdminUsers();
  } catch (err) {
    list.innerHTML = `<tr><td class="empty-row" colspan="5">${escapeHtml(err.message)}</td></tr>`;
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
    const providers = user.provider_ids?.length ? user.provider_ids.join(', ') : 'Email';
    const status = user.disabled ? 'Disabled' : 'Active';
    const actionButton = user.disabled ? 'Enable' : 'Disable';
    const action = user.disabled ? 'enable' : 'disable';
    const uid = escapeHtml(user.uid);
    return `
      <tr>
        <td>${escapeHtml(user.display_name || '—')}</td>
        <td>${escapeHtml(user.email || '—')}</td>
        <td>${escapeHtml(providers)}</td>
        <td>${status}</td>
        <td class="table-actions">
          <button class="btn-secondary btn-small" data-action="${action}" data-uid="${uid}">${actionButton}</button>
          <button class="btn-danger btn-small" data-action="delete" data-uid="${uid}">Delete</button>
        </td>
      </tr>`;
  }).join('');
}

async function modifyAdminUser(uid, action) {
  try {
    const headers = await authHeaders({ 'Content-Type': 'application/json' });
    const res = await fetch(`${API}/admin/users/${uid}`, {
      method: 'PATCH',
      headers,
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
    const headers = await authHeaders();
    const res = await fetch(`${API}/admin/settings`, { headers });
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
    const headers = await authHeaders({ 'Content-Type': 'application/json' });
    const res = await fetch(`${API}/admin/settings`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload)
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to save settings.', 'error');
      return;
    }
    state.admin.settings = body;
    await loadPersistedResults();
    if (state.modelTrained) initAlerts();
    showToast('Admin settings saved and risk tiers recalculated.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function loadAdminModelStatus() {
  try {
    const headers = await authHeaders();
    const res = await fetch(`${API}/admin/model`, { headers });
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
    const headers = await authHeaders({ 'Content-Type': 'application/json' });
    const res = await fetch(`${API}/admin/model/actions`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ action })
    });
    const body = await res.json();
    if (!res.ok) {
      showToast(body.error || 'Unable to perform model action.', 'error');
      return;
    }
    showToast(`Model action ${action} completed.`, 'success');
    if (action === 'reload_sample') await loadPersistedResults();
    if (action === 'reset') {
      state.modelTrained = false;
      state.results = null;
      state.allCustomers = [];
    }
    await loadAdminModelStatus();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function handleSignOut() {
  try {
    await fb.signOut(fb.auth);
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
$('export-overview-btn')?.addEventListener('click', () => exportCustomersCsv(state.allCustomers));
$('export-customers-btn')?.addEventListener('click', () => exportCustomersCsv(state.filteredCustomers, 'filtered '));

initTheme();
initPalette({
  navigate: requestNavigate,
  focusSearch() {
    if (!state.modelTrained) { showToast('Train a model to search customers.', 'error'); return; }
    navigateTo('customers');
    setTimeout(() => $('customer-search').focus(), prefersReducedMotion() ? 0 : 220);
  },
  toggleTheme: () => toggleTheme(),
  findCustomers(query) {
    const q = query.toLowerCase();
    return state.allCustomers.filter(c => c._q?.includes(q));
  },
  openCustomer(id) {
    navigateTo('customers');
    showCustomerDrawer(id);
  },
  getCommands() {
    const nav = Object.entries(titles)
      .filter(([key]) => key !== 'admin' || state.admin.isAdmin)
      .map(([key, label]) => ({
        id: `go:${key}`, label: `Go to ${label}`, group: 'Pages', keywords: key,
        run: () => requestNavigate(key),
      }));
    return [
      ...nav,
      { id: 'theme', label: 'Toggle dark theme', group: 'Actions', hint: 'D', keywords: 'light dark mode appearance', run: () => toggleTheme() },
      { id: 'export', label: 'Export risk list as CSV', group: 'Actions', keywords: 'download', run: () => exportCustomersCsv(state.allCustomers) },
      { id: 'retrain', label: 'Retrain model', group: 'Actions', keywords: 'upload train', run: () => navigateTo('upload') },
      { id: 'high', label: 'Show high-risk customers', group: 'Actions', keywords: 'filter churn', run: () => {
        if (!state.modelTrained) { showToast('Please upload data and train a model first.', 'error'); return; }
        state.riskFilter = 'High';
        state.currentPage = 1;
        document.querySelectorAll('#risk-filters .risk-filter').forEach(b => b.classList.toggle('active', b.dataset.risk === 'High'));
        renderCustomerTable();
        navigateTo('customers');
      } },
      ...(state.user ? [{ id: 'signout', label: 'Sign out', group: 'Actions', run: handleSignOut }] : []),
    ];
  },
});

// Fonts and late layout shifts move the nav items; keep the indicator aligned.
document.fonts?.ready.then(moveNavIndicator);
window.addEventListener('load', moveNavIndicator);

initAuthUI();
navigateTo('upload');
