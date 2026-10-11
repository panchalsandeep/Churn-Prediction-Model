/* Settings & admin: model registry, economics/thresholds, drift monitoring, audit log, user management. */
import { $, state, escapeHtml, showToast, fmtInt, humanize, setOverlay, timeAgo, on, can } from '../lib/ctx.js';
import { get, post, patch, runJob } from '../lib/api.js';
import { loadTeam, initTeam } from './team.js';

let callbacks = { reloadResults: async () => {}, navigate: () => {} };
let users = [];

/* ── Registry ───────────────────────────────────────────────── */
async function loadRegistry() {
  const body = $('registry-body');
  try {
    const { models } = await get('/models');
    $('admin-model-status').textContent = models.some(m => m.status === 'champion') ? 'Live' : 'None';
    body.innerHTML = models.length ? models.map((m, i) => `
      <tr>
        <td><code>v${models.length - i}</code> <small class="muted">${escapeHtml(m.id.slice(0, 6))}</small></td>
        <td><span class="risk-tag ${m.status === 'champion' ? 'Low' : m.status === 'challenger' ? 'Medium' : ''}">${escapeHtml(m.status)}</span></td>
        <td>${escapeHtml(m.algorithm || '–')}${m.source === 'demo' ? ' <small class="muted">demo</small>' : ''}</td>
        <td>${m.pr_auc != null ? m.pr_auc.toFixed(1) : '–'}</td><td>${m.roc_auc != null ? m.roc_auc.toFixed(1) : '–'}</td>
        <td>${fmtInt(m.rows)}</td><td>${timeAgo(m.created_at)}</td>
        <td>${m.status === 'champion' ? '' : `<button class="btn-secondary btn-small" data-promote="${escapeHtml(m.id)}" type="button">Promote</button>`}</td>
      </tr>`).join('') : '<tr><td class="empty-row" colspan="8">No models yet.</td></tr>';
  } catch (err) {
    body.innerHTML = `<tr><td class="empty-row" colspan="8">${escapeHtml(err.message)}</td></tr>`;
  }
}

/* ── Settings ───────────────────────────────────────────────── */
function syncManualVisibility() {
  $('manual-thresholds').hidden = $('set-mode').value !== 'manual';
}

async function loadSettings() {
  try {
    const s = await get('/settings');
    $('set-cost').value = s.economics.outreach_cost;
    $('set-save').value = s.economics.save_rate;
    $('set-months').value = s.economics.value_months;
    $('set-mode').value = s.thresholds.mode;
    const eff = s.effective_thresholds;
    $('set-high').value = s.thresholds.high ?? eff?.high ?? '';
    $('set-medium').value = s.thresholds.medium ?? eff?.medium ?? '';
    $('set-alert-high').checked = s.alert_rules.high_risk.enabled;
    $('set-alert-jump').checked = s.alert_rules.risk_jump.enabled;
    $('set-jump').value = s.alert_rules.risk_jump.delta;
    $('threshold-note').textContent = eff
      ? `Currently: high ≥ ${(eff.high * 100).toFixed(0)}%, medium ≥ ${(eff.medium * 100).toFixed(0)}%. Break-even probability ${(eff.breakeven * 100).toFixed(0)}%: below it, an average outreach costs more than it saves.`
      : 'Thresholds appear once a model is trained.';
    syncManualVisibility();
  } catch { /* no model yet */ }
}

async function saveSettings() {
  const mode = $('set-mode').value;
  const payload = {
    economics: { outreach_cost: $('set-cost').value, save_rate: $('set-save').value, value_months: $('set-months').value },
    thresholds: { mode, ...(mode === 'manual' ? { high: $('set-high').value, medium: $('set-medium').value } : {}) },
    alert_rules: { high_risk: { enabled: $('set-alert-high').checked }, risk_jump: { enabled: $('set-alert-jump').checked, delta: Number($('set-jump').value) || 0.15 } },
  };
  try {
    await post('/settings', payload);
    await callbacks.reloadResults();
    await loadSettings();
    showToast('Saved. Tiers, alerts and segments were recalculated.');
  } catch (err) { showToast(err.message, 'error'); }
}

/* ── Drift ──────────────────────────────────────────────────── */
async function loadDrift() {
  const panel = $('drift-panel');
  try {
    const { drift } = await get('/monitoring/drift');
    if (!drift) { panel.innerHTML = '<div class="empty-card">No batch scored yet. Use “Score a new batch” to compare fresh data with the training data.</div>'; return; }
    const label = { stable: 'Stable', moderate_shift: 'Moderate shift', major_shift: 'Major shift' }[drift.status];
    const cls = drift.status === 'stable' ? 'Low' : drift.status === 'moderate_shift' ? 'Medium' : 'High';
    const bar = v => `<span class="psi-track"><span class="psi-fill ${v > 0.25 ? 'High' : v > 0.1 ? 'Medium' : 'Low'}" style="--w:${Math.min(100, (v / 0.5) * 100)}%"></span></span>`;
    panel.innerHTML = `
      <div class="drift-head"><span class="risk-tag ${cls}">${label}</span><small>${fmtInt(drift.rows)} customers · ${timeAgo(drift.scored_at)}</small></div>
      <div class="drift-row"><span>Score distribution</span>${bar(drift.score_psi)}<b>${drift.score_psi.toFixed(3)}</b></div>
      ${drift.features.slice(0, 6).map(f => `<div class="drift-row"><span>${escapeHtml(humanize(f.feature))}</span>${bar(f.psi)}<b>${f.psi.toFixed(3)}</b></div>`).join('')}
      <p class="card-subtitle">PSI &lt; 0.1 stable · 0.1–0.25 moderate · &gt; 0.25 major. Retrain when features or scores shift materially.</p>`;
  } catch { /* ignore */ }
}

/* ── Users (Firebase) ───────────────────────────────────────── */
async function loadUsers() {
  const list = $('admin-user-list');
  if (!state.admin.isAdmin) return;
  try {
    const body = await get('/admin/users');
    users = body.users || [];
    $('admin-user-count').textContent = users.length;
    renderUsers();
  } catch (err) {
    list.innerHTML = `<tr><td class="empty-row" colspan="5">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderUsers() {
  const list = $('admin-user-list');
  const q = $('admin-user-search')?.value?.toLowerCase() || '';
  const rows = users.filter(u => !q || [u.email, u.display_name, u.uid].some(v => v && v.toLowerCase().includes(q)));
  list.innerHTML = rows.length ? rows.map(u => {
    const uid = escapeHtml(u.uid);
    const action = u.disabled ? 'enable' : 'disable';
    return `<tr><td>${escapeHtml(u.display_name || '—')}</td><td>${escapeHtml(u.email || '—')}</td>
      <td>${escapeHtml(u.provider_ids?.join(', ') || 'Email')}</td><td>${u.disabled ? 'Disabled' : 'Active'}</td>
      <td class="table-actions"><button class="btn-secondary btn-small" data-action="${action}" data-uid="${uid}" type="button">${u.disabled ? 'Enable' : 'Disable'}</button>
      <button class="btn-danger btn-small" data-action="delete" data-uid="${uid}" type="button">Delete</button></td></tr>`;
  }).join('') : '<tr><td class="empty-row" colspan="5">No users found.</td></tr>';
}

/* ── Public API ─────────────────────────────────────────────── */
export async function loadAdminPanel() {
  const r = state.results;
  $('admin-customer-count').textContent = r ? fmtInt(r.summary.total_customers) : '—';
  $('users-card').hidden = !state.admin.isAdmin;
  $('workspace-badge').textContent = state.me.role;
  // Read-only roles can look at settings but not change them.
  document.querySelectorAll('#admin-settings-form input, #admin-settings-form select').forEach(el => { el.disabled = !can('admin'); });
  await Promise.all([loadRegistry(), loadSettings(), loadDrift(), loadUsers(), loadTeam()]);
}

export function initAdmin(cb) {
  callbacks = cb;
  initTeam({ reloadResults: cb.reloadResults });
  $('set-mode').addEventListener('change', syncManualVisibility);
  $('admin-save-settings-btn').addEventListener('click', saveSettings);
  $('admin-refresh-btn').addEventListener('click', loadAdminPanel);
  $('admin-user-search')?.addEventListener('input', renderUsers);

  $('registry-body').addEventListener('click', async e => {
    const btn = e.target.closest('[data-promote]');
    if (!btn) return;
    $('loading-text').textContent = 'Promoting model and restoring its scores…';
    setOverlay($('loading-overlay'), true);
    try {
      await post(`/models/${btn.dataset.promote}/promote`, {});
      await callbacks.reloadResults();
      await loadAdminPanel();
      showToast('Model promoted.');
    } catch (err) { showToast(err.message, 'error'); } finally { setOverlay($('loading-overlay'), false); }
  });

  $('admin-score-btn').addEventListener('click', () => $('score-file').click());
  $('score-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    $('loading-text').textContent = 'Scoring batch and checking drift…';
    setOverlay($('loading-overlay'), true);
    try {
      const res = await runJob('/score', { form });
      await callbacks.reloadResults();
      await loadAdminPanel();
      showToast(`Scored ${fmtInt(res.rows)} customers. Drift: ${res.drift.status.replace('_', ' ')}.`);
    } catch (err) { showToast(err.message, 'error'); } finally { setOverlay($('loading-overlay'), false); }
  });

  $('admin-load-sample-btn').addEventListener('click', async () => {
    $('loading-text').textContent = 'Training on the demo dataset…';
    setOverlay($('loading-overlay'), true);
    try {
      await runJob('/demo', { json: { rows: 8000 } });
      await callbacks.reloadResults();
      await loadAdminPanel();
      showToast('Demo workspace loaded.');
    } catch (err) { showToast(err.message, 'error'); } finally { setOverlay($('loading-overlay'), false); }
  });

  $('admin-reset-model-btn').addEventListener('click', async () => {
    if (!window.confirm('Delete all models, scores, alerts and logged activity in this workspace? This cannot be undone.')) return;
    try {
      await post('/workspace/reset', {});
      await callbacks.reloadResults();
      callbacks.navigate('upload');
      showToast('Workspace reset.');
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('admin-user-table')?.addEventListener('click', async e => {
    const btn = e.target.closest('button[data-uid]');
    if (!btn) return;
    try {
      await patch(`/admin/users/${btn.dataset.uid}`, { action: btn.dataset.action });
      showToast(`User ${btn.dataset.action}d.`);
      loadUsers();
    } catch (err) { showToast(err.message, 'error'); }
  });

  on('results', () => { if (state.currentSection === 'admin') loadAdminPanel(); });
}

