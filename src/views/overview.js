/* Overview: KPIs, insight tiles, lift chart, risk donut, segments, model snapshot. */
import { $, state, escapeHtml, fmtInt, fmtPct, fmtMoney, fmtCompactMoney, humanize, replay, on, timeAgo } from '../lib/ctx.js';
import { animateNumber, prefersReducedMotion } from '../lib/motion.js';
import { registerCharts, makeChart, chartAnimation, tooltipStyle, gridStyle, axisTitle, legend } from '../lib/chartkit.js';

function renderKpis(r) {
  const { summary: s, metrics: m, confidence_intervals: ci, precision_at_k: pk } = r;
  animateNumber($('kpi-total-val'), s.total_customers, { duration: 900 });
  $('kpi-total-sub').textContent = s.observed_churn_rate != null ? `${fmtPct(s.observed_churn_rate)} churned in the data` : '';
  animateNumber($('kpi-avg-val'), s.avg_risk, { duration: 900, format: v => `${v.toFixed(1)}%` });
  $('kpi-avg-sub').textContent = 'calibrated probability';
  animateNumber($('kpi-high-val'), s.high_risk, { duration: 900 });
  $('kpi-high-sub').textContent = `${fmtCompactMoney(s.expected_loss_high)} expected loss`;
  animateNumber($('kpi-auc-val'), m.roc_auc, { duration: 1000, format: v => v.toFixed(1) });
  $('kpi-auc-sub').textContent = ci?.roc_auc ? `95% CI ${(ci.roc_auc[0] * 100).toFixed(0)}–${(ci.roc_auc[1] * 100).toFixed(0)}` : 'holdout';
  animateNumber($('kpi-pr-val'), m.pr_auc, { duration: 1000, format: v => v.toFixed(1) });
  $('kpi-pr-sub').textContent = `${(m.pr_auc / Math.max(m.pr_auc_baseline, 0.01)).toFixed(1)}× the ${m.pr_auc_baseline.toFixed(0)}% baseline`;
  const p10 = pk?.['10%'];
  animateNumber($('kpi-p10-val'), p10?.precision ?? 0, { duration: 1000, format: v => `${v.toFixed(0)}%` });
  $('kpi-p10-sub').textContent = p10 ? `finds ${p10.recall.toFixed(0)}% of churners` : '';
}

function renderInsights(r) {
  const s = r.summary;
  const driver = r.feature_importance?.[0];
  $('overview-lede').textContent = s.high_risk
    ? `${fmtInt(s.high_risk)} accounts need outreach. The model ranks churners ${r.lift[0].lift.toFixed(1)}× better than chance in the top decile.`
    : 'Portfolio looks stable. Keep monitoring usage and support signals.';
  $('insight-revenue').textContent = r.has_revenue ? fmtMoney(s.expected_loss_high) : `${fmtInt(s.high_risk)} accounts`;
  $('insight-revenue-meta').textContent = r.has_revenue
    ? `${fmtMoney(s.high_risk_monthly_revenue)}/mo across ${fmtInt(s.high_risk)} high-risk accounts`
    : 'No revenue column detected; ranking by risk only';
  $('insight-driver').textContent = driver ? driver.label : '—';
  $('insight-driver-meta').textContent = driver ? `${(driver.importance * 100).toFixed(0)}% of explained signal` : 'Train a model to rank drivers';
  const net = s.expected_net_value;
  $('insight-action').textContent = s.actionable_customers
    ? `Contact ${fmtInt(s.actionable_customers)} accounts`
    : 'No outreach pays off at current costs';
  $('insight-action-meta').textContent = s.actionable_customers
    ? `Expected net value ${fmtMoney(net)} after ${fmtMoney(s.economics.outreach_cost)} per touch`
    : 'Lower the outreach cost or raise the save rate in Admin';
}

function renderSegments(r) {
  const select = $('segment-attr');
  const groups = r.segments || [];
  const keep = select.value;
  select.innerHTML = groups.map(g => `<option value="${escapeHtml(g.attribute)}">${escapeHtml(humanize(g.attribute))}</option>`).join('');
  if (groups.some(g => g.attribute === keep)) select.value = keep;
  select.hidden = groups.length === 0;
  drawSegments(r);
}

function drawSegments(r) {
  const group = (r.segments || []).find(g => g.attribute === $('segment-attr').value) || (r.segments || [])[0];
  const body = $('segments-body');
  if (!group) { body.innerHTML = '<tr><td colspan="5" class="empty-row">No categorical columns to segment by.</td></tr>'; return; }
  const peak = Math.max(...group.segments.map(s => s.expected_loss), 1);
  body.innerHTML = group.segments.map(s => `
    <tr><td><strong>${escapeHtml(s.value)}</strong></td><td>${fmtInt(s.customers)}</td><td>${fmtPct(s.avg_risk)}</td>
    <td>${fmtInt(s.high_risk)}</td>
    <td><div class="loss-cell"><span class="loss-bar" style="--w:${(s.expected_loss / peak) * 100}%"></span>${r.has_revenue ? fmtCompactMoney(s.expected_loss) : s.expected_loss.toFixed(1)}</div></td></tr>`).join('');
}

function renderSnapshot(r) {
  const m = r.model;
  const best = [...m.comparison].sort((a, b) => b.cv_pr_auc - a.cv_pr_auc)[0];
  const row = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  $('model-snapshot').innerHTML = [
    row('Algorithm', `${escapeHtml(m.algorithm)} <small>won CV over ${m.comparison.length - 1} others</small>`),
    row('Training data', `${fmtInt(m.rows)} rows · ${m.feature_count} features`),
    row('Evaluation', m.split === 'time' ? 'Time-based holdout (latest 20%)' : 'Stratified random holdout (20%)'),
    row('Holdout size', fmtInt(m.test_rows)),
    row('Calibration', `${m.calibration_method === 'isotonic' ? 'Isotonic' : 'Platt (sigmoid)'} · Brier ${r.metrics.brier}`),
    row('Trained', `${timeAgo(m.created_at)}${m.source === 'demo' ? ' · demo data' : ''}`),
    row('Best CV PR-AUC', `${best.cv_pr_auc.toFixed(1)} ± ${best.cv_std.toFixed(1)}`),
  ].join('');
}

function buildLift(r) {
  const base = r.metrics.base_rate;
  makeChart('lift', 'liftChart', t => ({
    type: 'bar',
    data: {
      labels: r.lift.map(d => `D${d.decile}`),
      datasets: [
        { type: 'bar', label: 'Churn rate in decile (%)', data: r.lift.map(d => d.churn_rate),
          backgroundColor: r.lift.map(d => (d.churn_rate > base ? (t.dark ? 'rgba(226,107,107,0.85)' : 'rgba(194,59,59,0.8)') : (t.dark ? 'rgba(77,184,179,0.75)' : 'rgba(20,122,122,0.7)'))),
          borderRadius: 6, borderSkipped: false },
        { type: 'line', label: `Overall base rate (${base.toFixed(1)}%)`, data: r.lift.map(() => base), borderColor: t.reference,
          borderDash: [6, 4], pointRadius: 0, borderWidth: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: chartAnimation(1000),
      plugins: { legend: legend(), tooltip: { ...tooltipStyle(), callbacks: { afterBody: items => `Lift ${r.lift[items[0].dataIndex].lift}×` } } },
      scales: { x: { ...gridStyle(), title: axisTitle('Risk decile (D1 = riskiest)') },
                y: { ...gridStyle(), beginAtZero: true, title: axisTitle('Churn rate (%)'), ticks: { ...gridStyle().ticks, callback: v => `${v}%` } } },
    },
  }));
}

function buildDonut(r) {
  const s = r.summary;
  const labels = ['High risk', 'Medium risk', 'Low risk'];
  const data = [s.high_risk, s.medium_risk, s.low_risk];
  let colors;
  makeChart('donut', 'riskDonutChart', t => {
    colors = t.dark ? ['#E26B6B', '#F0993A', '#3FBF86'] : ['#C23B3B', '#D97706', '#1F8A5B'];
    return {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: 'transparent', hoverOffset: 8, borderWidth: 0 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '72%',
        animation: prefersReducedMotion() ? false : { animateRotate: true, animateScale: true, duration: 1100, easing: 'easeOutQuart' },
        plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      },
    };
  });
  const el = $('donut-legend');
  el.setAttribute('role', 'list');
  el.innerHTML = labels.map((l, i) => `<div class="legend-item" role="listitem"><div class="legend-dot" style="background:${colors[i]}"></div><span>${l}: <b>${fmtInt(data[i])}</b></span></div>`).join('');
}

export function initOverview() {
  registerCharts('overview', () => { buildLift(state.results); buildDonut(state.results); });
  $('segment-attr').addEventListener('change', () => drawSegments(state.results));
  on('results', r => {
    renderKpis(r);
    renderInsights(r);
    renderSegments(r);
    renderSnapshot(r);
    replay($('model-snapshot'), 'pop-in');
  });
}
