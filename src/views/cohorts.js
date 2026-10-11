/* Cohorts: real churn rate / predicted risk by cohort × tenure band, from the labelled data. */
import { $, qsa, state, escapeHtml, on } from '../lib/ctx.js';
import { stagger } from '../lib/motion.js';
import { registerCharts, markDirty, destroyChart, renderSectionCharts, makeChart, chartAnimation, tooltipStyle, gridStyle, axisTitle, legend } from '../lib/chartkit.js';

let metric = 'rate';

const quantile = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];

function current() {
  const key = $('cohort-type').value;
  return state.results?.cohorts?.[key];
}

function renderHeatmap() {
  const c = current();
  const grid = $('heatmap-grid');
  if (!c) { grid.innerHTML = '<div class="heatmap-empty">No cohort data for this dataset.</div>'; $('cohort-summary').textContent = ''; return; }
  const matrix = c[metric];
  const values = matrix.flat().filter(v => v != null).sort((a, b) => a - b);
  const cuts = values.length ? [0.2, 0.4, 0.6, 0.8].map(q => quantile(values, q)) : [];
  // Lower churn = greener. Level 5 is best.
  const level = v => (v <= cuts[0] ? 5 : v <= cuts[1] ? 4 : v <= cuts[2] ? 3 : v <= cuts[3] ? 2 : 1);

  grid.style.gridTemplateColumns = `minmax(110px, 1.2fr) repeat(${c.cols.length}, minmax(64px, 1fr))`;
  const head = `<div class="hm-corner"></div>${c.cols.map(col => `<div class="hm-head">${escapeHtml(col)}</div>`).join('')}`;
  const rows = c.rows.map((row, ri) => `
    <div class="hm-rowhead">${escapeHtml(row)}</div>
    ${c.cols.map((_, ci) => {
      const v = matrix[ri][ci];
      const n = c.count[ri][ci];
      return v == null
        ? `<div class="heatmap-cell hm-empty cell-in" style="--i:${Math.min(ri + ci, 14)}" title="Fewer than 10 customers (${n})">–</div>`
        : `<div class="heatmap-cell heat-${level(v)} cell-in" style="--i:${Math.min(ri + ci, 14)}" title="${n} customers"><strong>${v.toFixed(1)}%</strong><small>n=${n}</small></div>`;
    }).join('')}`).join('');
  grid.innerHTML = head + rows;
  $('cohort-summary').textContent = `${c.label}: ${c.rows.length} groups × ${c.cols.length} tenure bands. ${metric === 'rate' ? 'Observed churn in the labelled data.' : 'Mean model-predicted risk (out-of-fold).'}`;
}

function buildTrend() {
  const c = current();
  if (!c) return;
  makeChart('cohortTrend', 'cohortTrendChart', t => ({
    type: 'line',
    data: {
      labels: c.cols,
      datasets: c.rows.map((row, i) => ({
        label: row, data: c.rate[i].map(v => (v == null ? null : +(100 - v).toFixed(1))), spanGaps: true,
        borderColor: `hsl(${168 + i * 28}, ${t.dark ? 48 : 40}%, ${(t.dark ? 58 : 36) + (i % 3) * 4}%)`, tension: 0.3, pointRadius: 3, borderWidth: 2.5, fill: false,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: chartAnimation(1000),
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: legend(), tooltip: tooltipStyle() },
      scales: { x: { ...gridStyle(), title: axisTitle('Tenure band') }, y: { ...gridStyle(), title: axisTitle('Retained (%)'), ticks: { ...gridStyle().ticks, callback: v => `${v}%` } } },
    },
  }));
}

export function initCohorts() {
  registerCharts('cohorts', buildTrend);
  $('cohort-type').addEventListener('change', () => {
    renderHeatmap();
    destroyChart('cohortTrend');
    markDirty('cohorts');
    renderSectionCharts('cohorts');
  });
  qsa('#cohort-metric .alert-tab').forEach(btn => btn.addEventListener('click', () => {
    qsa('#cohort-metric .alert-tab').forEach(b => b.classList.toggle('active', b === btn));
    metric = btn.dataset.metric;
    renderHeatmap();
  }));
  on('results', r => {
    const select = $('cohort-type');
    const keep = select.value;
    select.innerHTML = Object.entries(r.cohorts).map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(v.label)}</option>`).join('');
    if (r.cohorts[keep]) select.value = keep;
    renderHeatmap();
    stagger(qsa('#heatmap-grid .heatmap-cell'), 14);
  });
}
