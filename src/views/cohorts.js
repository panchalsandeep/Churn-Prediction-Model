/* Cohorts: real churn rate / predicted risk by cohort × tenure band, from the labelled data. */
import { $, qsa, state, escapeHtml, humanize, on } from '../lib/ctx.js';
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
  const r = state.results;
  const key = $('cohort-type').value;
  const surv = r?.survival;
  if (surv) {
    // Real Kaplan–Meier curves; per-group when the selected grouping has them, otherwise the overall curve.
    const group = surv.groups?.[key];
    const curves = group ? group.curves.map(c => ({ name: `${c.name} (n=${c.n})`, t: c.t, s: c.s })) : [{ name: 'All customers', t: surv.overall.t, s: surv.overall.s }];
    $('survival-title').textContent = 'Survival by tenure (Kaplan–Meier)';
    const bits = [];
    if (surv.median != null) bits.push(`median lifetime ${surv.median.toFixed(0)} months`);
    if (surv.s12 != null) bits.push(`${(surv.s12 * 100).toFixed(0)}% still active after 12 months`);
    $('survival-note').textContent = `${bits.join(' · ') || 'Probability a customer is still active after each tenure.'}${group ? ` Split by ${group.label.toLowerCase()}.` : ''}`;
    makeChart('cohortTrend', 'cohortTrendChart', t => ({
      type: 'line',
      data: { datasets: curves.map((c, i) => ({
        label: c.name, data: c.t.map((x, j) => ({ x, y: +(c.s[j] * 100).toFixed(1) })), stepped: true, pointRadius: 0, borderWidth: 2.5, fill: false,
        borderColor: `hsl(${168 + i * 34}, ${t.dark ? 50 : 42}%, ${(t.dark ? 58 : 36) + (i % 3) * 4}%)`,
      })) },
      options: {
        responsive: true, maintainAspectRatio: false, animation: chartAnimation(1100), parsing: false,
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        plugins: { legend: legend(), tooltip: tooltipStyle() },
        scales: { x: { ...gridStyle(), type: 'linear', min: 0, title: axisTitle('Tenure (months)') },
                  y: { ...gridStyle(), min: 0, max: 100, title: axisTitle('Still active (%)'), ticks: { ...gridStyle().ticks, callback: v => `${v}%` } } },
      },
    }));
    return;
  }
  const c = current();
  if (!c) return;
  $('survival-title').textContent = 'Retention by tenure band';
  $('survival-note').textContent = 'Share of customers retained in each tenure band (no tenure column was found for survival curves).';
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

function renderPersonas(r) {
  const box = $('personas-grid');
  const list = r.personas || [];
  if (!list.length) { box.innerHTML = '<div class="empty-card">Not enough numeric variation to discover personas in this dataset.</div>'; return; }
  const label = p => {
    const t = p.traits.slice(0, 2).map(x => `${x.direction === 'high' ? 'High' : 'Low'} ${humanize(x.feature)}`);
    const c = p.categories[0] ? [`${humanize(p.categories[0].feature)}: ${p.categories[0].value}`] : [];
    return [...c, ...t].join(' · ') || 'Typical customers';
  };
  box.innerHTML = list.map((p, i) => `
    <article class="persona ${p.lift_vs_overall >= 1.3 ? 'hot' : p.lift_vs_overall <= 0.7 ? 'cool' : ''} card-in" style="--i:${i}">
      <header><span class="persona-no">${p.id}</span><h4>${escapeHtml(label(p))}</h4></header>
      <div class="persona-stats">
        <div><b>${p.share}%</b><span>of customers</span></div>
        <div><b>${p.churn_rate}%</b><span>churn · ${p.lift_vs_overall}× avg</span></div>
        <div><b>${p.avg_risk}%</b><span>predicted risk</span></div>
        ${p.revenue_share != null ? `<div><b>${p.revenue_share}%</b><span>of revenue</span></div>` : ''}
      </div>
      <ul class="persona-traits">
        ${p.traits.map(t => `<li><span class="trait-dir ${t.direction}">${t.direction === 'high' ? '▲' : '▼'}</span>${escapeHtml(humanize(t.feature))} <small>${t.value} vs ${t.overall} typical</small></li>`).join('')}
        ${p.categories.map(c => `<li><span class="trait-dir cat">●</span>${escapeHtml(humanize(c.feature))}: ${escapeHtml(c.value)} <small>${Math.round(c.share * 100)}% of group</small></li>`).join('')}
      </ul>
    </article>`).join('');
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
    renderPersonas(r);
    stagger(qsa('#heatmap-grid .heatmap-cell'), 14);
  });
}
