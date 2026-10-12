/* Analytics: gain, calibration, ROC, importance, score distribution, performance rings & tables. */
import { $, qsa, state, escapeHtml, fmtInt, fmtPct, on } from '../lib/ctx.js';
import { animateNumber, prefersReducedMotion } from '../lib/motion.js';
import { registerCharts, makeChart, chartAnimation, tooltipStyle, gridStyle, axisTitle, legend, chartTheme } from '../lib/chartkit.js';

let operatingPoint = 'economic';

function operating(r) {
  if (operatingPoint === 'f1') {
    return { m: { ...r.metrics, ...r.metrics_at_f1, threshold: r.metrics.threshold_f1 }, cm: r.confusion_matrix_f1,
             note: 'Threshold that maximises F1 on the holdout: balanced precision and recall.' };
  }
  return { m: r.metrics, cm: r.confusion_matrix,
           note: `Business threshold ${(r.metrics.threshold * 100).toFixed(0)}%: the high-risk tier cut-off derived from outreach cost and save value.` };
}

function renderOperating(r) {
  const { m, cm, note } = operating(r);
  $('op-note').textContent = note;
  $('cm-threshold').textContent = `at ${(m.threshold * 100).toFixed(1)}%`;
  [['cm-tn', cm[0][0]], ['cm-fp', cm[0][1]], ['cm-fn', cm[1][0]], ['cm-tp', cm[1][1]]].forEach(([id, v]) => animateNumber($(id), v, { duration: 600 }));
  $('ring-auc-val').textContent = fmtPct(r.metrics.roc_auc);
  $('ring-pr-val').textContent = fmtPct(r.metrics.pr_auc);
  $('ring-prec-val').textContent = fmtPct(m.precision);
  $('ring-rec-val').textContent = fmtPct(m.recall);
}

function renderTables(r) {
  $('pak-body').innerHTML = Object.entries(r.precision_at_k).map(([k, v]) =>
    `<tr><td><strong>${k}</strong></td><td>${fmtInt(v.customers)}</td><td>${fmtPct(v.precision)}</td><td>${fmtPct(v.recall)}</td></tr>`).join('');
  const best = Math.max(...r.model.comparison.map(c => c.cv_pr_auc));
  $('compare-body').innerHTML = r.model.comparison.map(c =>
    `<tr><td><strong>${escapeHtml(c.name)}</strong></td><td>${c.cv_pr_auc.toFixed(1)} ± ${c.cv_std.toFixed(1)}</td>
     <td>${c.cv_pr_auc === best ? '<span class="risk-tag Low">selected</span>' : ''}</td></tr>`).join('');
  const warnings = [...r.model.warnings];
  $('model-warnings').innerHTML = warnings.length
    ? `<h4 class="cm-title">Data warnings</h4><ul class="schema-warnings">${warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : '';
}

function buildGain(r) {
  makeChart('gain', 'gainChart', t => {
    const pts = [{ x: 0, y: 0 }, ...r.lift.map(d => ({ x: d.decile * 10, y: d.cumulative_capture }))];
    return {
      type: 'line',
      data: { datasets: [
        { label: 'Model', data: pts, borderColor: t.primary, backgroundColor: t.dark ? 'rgba(127,209,207,0.12)' : 'rgba(11,61,74,0.08)', fill: true, tension: 0.25, borderWidth: 2.5, pointRadius: 3 },
        { label: 'Random targeting', data: [{ x: 0, y: 0 }, { x: 100, y: 100 }], borderColor: t.reference, borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5 },
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: chartAnimation(1100), parsing: false,
        plugins: { legend: legend(), tooltip: { ...tooltipStyle(), callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y.toFixed(0)}% of churners at top ${c.parsed.x}%` } } },
        scales: { x: { ...gridStyle(), type: 'linear', min: 0, max: 100, title: axisTitle('Customers contacted (%) · riskiest first') },
                  y: { ...gridStyle(), min: 0, max: 100, title: axisTitle('Churners captured (%)') } },
      },
    };
  });
}

function buildCalibration(r) {
  makeChart('calibration', 'calibrationChart', t => {
    const pts = r.calibration.map(c => ({ x: c.predicted, y: c.observed }));
    const top = Math.max(10, Math.ceil(Math.max(...pts.map(p => Math.max(p.x, p.y))) / 10) * 10);
    return {
      type: 'line',
      data: { datasets: [
        { label: 'Model', data: pts, borderColor: t.dark ? '#E8A838' : '#C23B3B', backgroundColor: t.dark ? '#E8A838' : '#C23B3B', pointRadius: 5, borderWidth: 2.5, tension: 0.2 },
        { label: 'Perfectly calibrated', data: [{ x: 0, y: 0 }, { x: top, y: top }], borderColor: t.reference, borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5 },
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: chartAnimation(1000), parsing: false,
        plugins: { legend: legend(), tooltip: tooltipStyle() },
        scales: { x: { ...gridStyle(), type: 'linear', min: 0, max: top, title: axisTitle('Predicted churn probability (%)') },
                  y: { ...gridStyle(), min: 0, max: top, title: axisTitle('Observed churn rate (%)') } },
      },
    };
  });
}

function buildRoc(r) {
  const ci = r.confidence_intervals?.roc_auc;
  $('roc-note').textContent = `AUC ${r.metrics.roc_auc.toFixed(1)}%${ci ? ` (95% CI ${(ci[0] * 100).toFixed(1)}–${(ci[1] * 100).toFixed(1)}), bootstrapped on ${fmtInt(r.model.test_rows)} holdout customers` : ''}.`;
  makeChart('roc', 'rocChart', t => ({
    type: 'line',
    data: { datasets: [
      { label: 'ROC curve', data: r.roc_curve.fpr.map((x, i) => ({ x, y: r.roc_curve.tpr[i] })), borderColor: t.primary,
        backgroundColor: t.dark ? 'rgba(127,209,207,0.10)' : 'rgba(11,61,74,0.08)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2.5 },
      { label: 'Random', data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: t.reference, borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5 },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: chartAnimation(1200), parsing: false,
      plugins: { legend: legend(), tooltip: tooltipStyle() },
      scales: { x: { ...gridStyle(), type: 'linear', min: 0, max: 1, title: axisTitle('False positive rate') },
                y: { ...gridStyle(), min: 0, max: 1, title: axisTitle('True positive rate') } },
    },
  }));
}

function buildImportance(r) {
  const top = r.feature_importance.slice(0, 10);
  makeChart('feature', 'featureChart', t => ({
    type: 'bar',
    data: { labels: top.map(f => f.label), datasets: [{
      label: 'Share of explained signal', data: top.map(f => +(f.importance * 100).toFixed(1)),
      backgroundColor: top.map((_, i) => `hsla(${168 + i * 8}, 42%, ${t.dark ? 58 : 36}%, 0.85)`), borderRadius: 6, borderSkipped: false }] },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, animation: chartAnimation(900),
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: { x: { ...gridStyle(), title: axisTitle('% of total importance') }, y: gridStyle() },
    },
  }));
}

function buildScoreDist(r) {
  const d = r.score_distribution;
  makeChart('scoreDist', 'scoreDistChart', t => ({
    type: 'bar',
    data: { labels: d.labels.map(l => `${l.split('-')[0]}%`), datasets: [{ label: 'Customers', data: d.counts,
      backgroundColor: d.labels.map((_, i) => `rgba(${Math.round(20 + (i / 19) * 175)},${Math.round(122 - (i / 19) * 62)},${Math.round(122 - (i / 19) * 70)},0.78)`), borderRadius: 4, borderSkipped: false }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: chartAnimation(900),
      plugins: { legend: { display: false }, tooltip: tooltipStyle() },
      scales: { x: { ...gridStyle(), title: axisTitle('Predicted churn probability (holdout)') }, y: { ...gridStyle(), title: axisTitle('Customers') } },
    },
  }));
}

function buildRings(r) {
  const { m } = operating(r);
  const t0 = chartTheme();
  const rings = [
    ['ringAucCanvas', r.metrics.roc_auc, t0.dark ? '#3FBF86' : '#1F8A5B'],
    ['ringPrCanvas', r.metrics.pr_auc, t0.dark ? '#6B9BFF' : '#2F6FED'],
    ['ringPrecCanvas', m.precision, t0.dark ? '#F0993A' : '#D97706'],
    ['ringRecCanvas', m.recall, t0.dark ? '#4DB8B3' : '#147A7A'],
  ];
  rings.forEach(([canvas, value, color], i) => makeChart(canvas, canvas, t => ({
    type: 'doughnut',
    data: { datasets: [{ data: [value, 100 - value], backgroundColor: [color, t.track], borderWidth: 0, hoverOffset: 0 }] },
    options: {
      responsive: false, cutout: '78%',
      animation: prefersReducedMotion() ? false : { animateRotate: true, duration: 1000, delay: i * 120, easing: 'easeOutQuart' },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
    },
  })));
}

export function initAnalytics() {
  registerCharts('analytics', () => {
    const r = state.results;
    buildGain(r); buildCalibration(r); buildRoc(r); buildImportance(r); buildScoreDist(r); buildRings(r);
  });
  qsa('#op-toggle .alert-tab').forEach(btn => btn.addEventListener('click', () => {
    qsa('#op-toggle .alert-tab').forEach(b => b.classList.toggle('active', b === btn));
    operatingPoint = btn.dataset.op;
    renderOperating(state.results);
    buildRings(state.results);
  }));
  on('results', r => { renderOperating(r); renderTables(r); });
}
