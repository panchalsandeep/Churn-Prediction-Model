/* Alerts: persisted workflow (Open → Acknowledged → Resolved) and closed-loop intervention results. */
import { $, qsa, state, escapeHtml, fmtMoney, fmtInt, showToast, on, timeAgo, can } from '../lib/ctx.js';
import { get, patch } from '../lib/api.js';
import { prefersReducedMotion } from '../lib/motion.js';
import { openCustomer } from './drawer.js';

let tab = 'Open';

async function loadAlerts() {
  if (!state.results) return;
  try {
    const data = await get(`/alerts?status=${tab}&limit=100`);
    ['Open', 'Acknowledged', 'Resolved'].forEach(s => { $(`count-${s}`).textContent = fmtInt(data.counts[s] || 0); });
    const n = data.counts[tab] || 0;
    $('alert-summary').textContent = `${fmtInt(n)} ${tab.toLowerCase()} alert${n === 1 ? '' : 's'}${data.alerts.length < n ? `, showing the ${data.alerts.length} with the most revenue at risk` : ''}.`;
    render(data.alerts);
  } catch (err) {
    $('alerts-list').innerHTML = `<div class="empty-card">${escapeHtml(err.message)}</div>`;
  }
}

function render(items) {
  const hasRev = state.results?.has_revenue;
  $('alerts-list').innerHTML = items.length ? items.map((a, idx) => `
    <div class="alert-card card-in" style="--i:${Math.min(idx, 10)}" data-alert-id="${escapeHtml(a.id)}" data-customer-id="${escapeHtml(a.customer_id)}">
      <div class="alert-meta">
        <div>
          <div class="alert-title">${escapeHtml(a.title)}</div>
          <div class="alert-sub"><button class="link-btn" data-action="open" type="button">${escapeHtml(a.customer_id)}</button> · ${timeAgo(a.created_at)}${a.expected_loss ? ` · ${hasRev ? fmtMoney(a.expected_loss) : a.expected_loss.toFixed(1)} at risk` : ''}</div>
        </div>
        <span class="alert-badge ${escapeHtml(a.severity)}">${escapeHtml(a.severity)}</span>
      </div>
      <p>${escapeHtml(a.details || '')}</p>
      <div class="alert-badges">${(a.signals || []).map(s => `<span class="alert-badge sig">${escapeHtml(s)}</span>`).join('')}
        ${a.assignee ? `<span class="alert-badge assignee">Assigned: ${escapeHtml(a.assignee)}</span>` : ''}</div>
      <div class="alert-actions" ${can('analyst') ? '' : 'hidden'}>
        ${a.status === 'Open' ? '<button class="btn-secondary" data-action="acknowledge" type="button">Acknowledge</button>' : ''}
        ${a.status === 'Acknowledged' ? '<button class="btn-secondary" data-action="resolve" type="button">Mark resolved</button>' : ''}
        ${a.status !== 'Resolved' && !a.assignee ? '<button class="btn-secondary" data-action="assign" type="button">Assign to me</button>' : ''}
        ${a.status === 'Resolved' ? '<button class="btn-secondary" data-action="reopen" type="button">Reopen</button>' : ''}
      </div>
    </div>`).join('') : '<div class="empty-card">Nothing here. High-risk accounts and sudden risk jumps appear as alerts after each scoring run.</div>';
}

export async function loadLoop() {
  if (!state.results) return;
  try {
    const s = await get('/interventions/summary');
    $('loop-summary').innerHTML = s.total ? `
      <div class="loop-kpis"><div><b>${fmtInt(s.total)}</b><span>actions logged</span></div>
        <div><b>${s.save_rate == null ? '–' : `${s.save_rate}%`}</b><span>save rate (${fmtInt(s.resolved)} resolved)</span></div></div>
      <table class="customer-table compact"><thead><tr><th>Action</th><th>Total</th><th>Saved</th><th>Churned</th><th>Save rate</th></tr></thead><tbody>
      ${s.by_kind.map(k => `<tr><td><strong>${escapeHtml(k.kind)}</strong></td><td>${k.total}</td><td>${k.saved}</td><td>${k.churned}</td><td>${k.save_rate == null ? '–' : `${k.save_rate}%`}</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty-card">No actions logged yet. Open a customer and log an outreach.</div>';
  } catch { /* summary is optional */ }
}

export function refreshAlerts() {
  loadAlerts();
  loadLoop();
}

const NEXT = { acknowledge: 'Acknowledged', resolve: 'Resolved', reopen: 'Open' };

export function initAlerts() {
  qsa('#alert-tabs .alert-tab').forEach(btn => btn.addEventListener('click', () => {
    tab = btn.dataset.tab;
    qsa('#alert-tabs .alert-tab').forEach(b => b.classList.toggle('active', b === btn));
    loadAlerts();
  }));

  $('alerts-list').addEventListener('click', async e => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const card = btn.closest('.alert-card');
    const action = btn.dataset.action;
    if (action === 'open') { openCustomer(card.dataset.customerId, btn); return; }
    try {
      if (action === 'assign') {
        await patch(`/alerts/${card.dataset.alertId}`, { assignee: '@me' });
        showToast('Assigned to you.');
        loadAlerts();
        return;
      }
      await patch(`/alerts/${card.dataset.alertId}`, { status: NEXT[action] });
      const done = () => { loadAlerts(); showToast(`Alert moved to ${NEXT[action]}.`); };
      if (prefersReducedMotion()) { done(); return; }
      card.classList.add('card-out');
      card.addEventListener('animationend', done, { once: true });
    } catch (err) { showToast(err.message, 'error'); }
  });

  on('results', () => { loadAlerts(); loadLoop(); });
  on('interventions', loadLoop);
}
