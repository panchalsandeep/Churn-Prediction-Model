/* Model operations: realized performance, challenger in shadow, retraining policy, backtest, rollback. Server-gated by role. */
import { $, escapeHtml, showToast, fmtInt, fmtPct, timeAgo, can, setBusy } from '../lib/ctx.js';
import { get, post, put, request, runJob } from '../lib/api.js';

const HEALTH_TONE = { ok: 'Low', watch: 'Medium', degraded: 'High', insufficient: '' };
const HEALTH_LABEL = { ok: 'Healthy', watch: 'Watch', degraded: 'Degraded', insufficient: 'Not enough outcomes yet' };
const num = (v, d = 1) => (v == null ? '–' : Number(v).toFixed(d));

function renderHealth(h) {
  const badge = $('mlops-health');
  badge.textContent = HEALTH_LABEL[h.status] || h.status;
  badge.className = `risk-tag ${HEALTH_TONE[h.status] || ''}`;
  $('mlops-health-text').innerHTML = (h.reasons || []).map(r => `<p>${escapeHtml(r)}</p>`).join('')
    + (h.realized_roc_auc != null ? `<p class="muted">Realized ROC-AUC ${num(h.realized_roc_auc)} (promised ${num(h.expected_roc_auc)}), predicted vs observed ${num(h.calibration_ratio, 2)}×, over ${fmtInt(h.positives)} churn events in ${h.runs} run(s).</p>` : '');
}

function renderRuns(runs) {
  $('mlops-runs').innerHTML = runs.length ? runs.map(r => {
    const rz = r.realized;
    return `<tr><td>${escapeHtml(r.kind)}${r.kind === 'shadow' ? ' <small class="muted">(not shown to users)</small>' : ''}</td>
      <td>${escapeHtml(r.as_of || timeAgo(r.scored_at))}</td><td>${fmtInt(r.rows)}</td>
      <td>${r.status === 'resolved' ? '<span class="risk-tag Low">resolved</span>' : r.horizon_days ? `<span class="risk-tag">open · ${r.horizon_days}d horizon</span>` : '<span class="risk-tag Medium">needs outcomes</span>'}</td>
      <td>${rz ? `${num(rz.roc_auc)} / ${num(rz.pr_auc)}` : '–'}</td>
      <td>${rz ? `${fmtInt(rz.positives)} of ${fmtInt(rz.n)}` : '–'}</td>
      <td>${r.status === 'open' && !r.horizon_days && r.kind === 'live' ? `<label class="btn-secondary btn-small file-btn">Upload outcomes<input type="file" accept=".csv" data-outcomes="${r.id}" hidden /></label>` : ''}</td></tr>`;
  }).join('') : '<tr><td class="empty-row" colspan="7">No scoring runs yet.</td></tr>';
}

function renderShadow(s, challenger) {
  const box = $('mlops-shadow');
  if (!challenger) { box.innerHTML = '<p class="muted">No challenger. A new model is only ever trained as a challenger and must beat the live one on real outcomes before it replaces it.</p>'; return; }
  const d = s && s.challenger === challenger.id ? s : null;
  box.innerHTML = `<p><b>Challenger</b> ${escapeHtml(challenger.algorithm || '')} · PR-AUC ${num(challenger.pr_auc)} at training · created ${timeAgo(challenger.created_at)}</p>
    ${d ? `<p>Decision so far: <b>${escapeHtml(d.decision.replace('_', ' '))}</b>${d.matched_runs != null ? ` after ${d.matched_runs} matched run(s)` : ''}.</p>${(d.reasons || []).map(r => `<p class="muted">${escapeHtml(r)}</p>`).join('')}` : '<p class="muted">Being scored in shadow on every run; judged once enough outcomes are known.</p>'}
    <button class="btn-secondary btn-small" id="mlops-promote" type="button">Promote now (skip the wait)</button>`;
}

function renderBacktest(b) {
  const box = $('mlops-backtest');
  if (!b) { box.innerHTML = '<p class="muted">Not run yet. A backtest retrains at several past dates and shows how it would have done on what happened next.</p>'; return; }
  const s = b.summary;
  box.innerHTML = `${s ? `<p>Mean ROC-AUC <b>${num(s.roc_auc_mean)}</b> ± ${num(s.roc_auc_std)} (worst ${num(s.roc_auc_min)}), mean PR-AUC <b>${num(s.pr_auc_mean)}</b>, top-decile lift ${num(s.lift_top10_mean, 2)}× over ${s.origins} dates.</p>` : '<p class="muted">No usable origins.</p>'}
    <div class="table-wrapper"><table class="customer-table"><thead><tr><th>Origin</th><th>Trained on</th><th>Churn rate</th><th>ROC-AUC</th><th>PR-AUC</th><th>Lift</th></tr></thead><tbody>${
  b.results.map(r => `<tr><td>${escapeHtml(r.origin)}</td><td>${fmtInt(r.train_rows)} rows</td><td>${r.base_rate == null ? '–' : fmtPct(r.base_rate * 100)}</td><td>${num(r.roc_auc)}</td><td>${num(r.pr_auc)}</td><td>${num(r.lift_top10, 2)}×</td></tr>`).join('')}</tbody></table></div>
    ${b.skipped.map(x => `<p class="muted">Skipped ${escapeHtml(x.origin)}: ${escapeHtml(x.reason)}.</p>`).join('')}`;
}

function renderPolicy(p, auto) {
  for (const k of ['enabled', 'on_drift', 'on_degradation', 'auto_promote']) $(`pol-${k}`).checked = !!p[k];
  $('pol-every_days').value = p.every_days;
  $('pol-min_new_positives').value = p.min_new_positives;
  $('pol-shadow_min_runs').value = p.shadow_min_runs;
  $('pol-margin_pr_auc').value = p.margin_pr_auc;
  $('mlops-auto-note').hidden = auto;
}

export async function loadMlops() {
  if (!can('admin')) return;
  try {
    const m = await get('/mlops');
    renderHealth(m.health); renderRuns(m.runs); renderShadow(m.shadow, m.challenger); renderBacktest(m.backtest); renderPolicy(m.policy, m.retrains_automatically);
    $('mlops-rollback').disabled = !m.previous_champion;
  } catch (err) { showToast(err.message, 'error'); }
}

export function initMlops({ reloadResults }) {
  const job = (id, path, json, label, done) => $(id).addEventListener('click', async () => {
    const btn = $(id);
    setBusy(btn, true, label);
    try { await runJob(path, { json }); showToast(done); await loadMlops(); await reloadResults?.(); } catch (err) { showToast(err.message, 'error'); } finally { setBusy(btn, false); }
  });
  job('mlops-check', '/mlops/check', {}, 'Checking…', 'Check finished.');
  job('mlops-retrain', '/mlops/check', { force: true }, 'Training…', 'Challenger trained and now shadowing the live model.');
  job('mlops-backtest-btn', '/mlops/backtest', { origins: 4 }, 'Backtesting…', 'Backtest finished.');

  $('mlops-rollback').addEventListener('click', async () => {
    if (!window.confirm('Put the previous champion model back in front of users?')) return;
    try { await post('/mlops/rollback', {}); showToast('Rolled back.'); await loadMlops(); await reloadResults?.(); } catch (err) { showToast(err.message, 'error'); }
  });
  $('mlops-shadow').addEventListener('click', async e => {
    if (!e.target.closest('#mlops-promote') || !window.confirm('Promote the challenger now without waiting for realized outcomes?')) return;
    try { await post('/mlops/promote', {}); showToast('Challenger promoted.'); await loadMlops(); await reloadResults?.(); } catch (err) { showToast(err.message, 'error'); }
  });
  $('mlops-runs').addEventListener('change', async e => {
    const input = e.target.closest('[data-outcomes]');
    if (!input?.files[0]) return;
    const form = new FormData();
    form.append('file', input.files[0]);
    try { const r = await request(`/runs/${input.dataset.outcomes}/outcomes`, { method: 'POST', form }); showToast(`Realized ROC-AUC ${num(r.realized.roc_auc)}.`); await loadMlops(); }
    catch (err) { showToast(err.message, 'error'); }
  });
  $('mlops-policy-save').addEventListener('click', async () => {
    const body = { enabled: $('pol-enabled').checked, on_drift: $('pol-on_drift').checked, on_degradation: $('pol-on_degradation').checked, auto_promote: $('pol-auto_promote').checked,
      every_days: Number($('pol-every_days').value), min_new_positives: Number($('pol-min_new_positives').value), shadow_min_runs: Number($('pol-shadow_min_runs').value), margin_pr_auc: Number($('pol-margin_pr_auc').value) };
    try { await put('/mlops/policy', body); showToast('Policy saved.'); await loadMlops(); } catch (err) { showToast(err.message, 'error'); }
  });
}
