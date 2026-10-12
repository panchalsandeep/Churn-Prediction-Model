/* Customer 360 drawer: score, reasons, recommended actions, activity log (closed loop). */
import { $, state, escapeHtml, setOverlay, showToast, fmtMoney, humanize, emit, timeAgo } from '../lib/ctx.js';
import { get, post, patch } from '../lib/api.js';
import { animateNumber, stagger } from '../lib/motion.js';
import { renderImpacts } from '../lib/whatif.js';

let returnFocus = null;
let currentId = null;
let simulate = () => {};

const OUTCOMES = [['pending', 'Pending'], ['saved', 'Saved'], ['churned', 'Churned'], ['no_effect', 'No effect']];

function renderActivity(items) {
  $('drawer-activity').innerHTML = items.length ? items.map(i => `
    <li class="activity-item">
      <div><strong>${escapeHtml(i.kind)}</strong> <small>${timeAgo(i.created_at)} · ${escapeHtml(i.actor || 'unknown')}</small>
        ${i.notes ? `<p>${escapeHtml(i.notes)}</p>` : ''}</div>
      <select class="outcome-select" data-id="${escapeHtml(i.id)}" aria-label="Outcome">
        ${OUTCOMES.map(([v, l]) => `<option value="${v}" ${i.outcome === v ? 'selected' : ''}>${l}</option>`).join('')}
      </select>
    </li>`).join('') : '<li class="activity-empty">No actions logged yet.</li>';
}

export async function openCustomer(id, trigger) {
  const drawer = $('customer-drawer');
  returnFocus = trigger || document.activeElement;
  currentId = id;
  $('drawer-customer-id').textContent = id;
  setOverlay(drawer, true);
  $('drawer-close').focus({ preventScroll: true });
  $('drawer-reasons').innerHTML = '<div class="skeleton" style="width:80%"></div>';
  try {
    const d = await get(`/customers/${encodeURIComponent(id)}`);
    if (currentId !== id) return;
    const hasRev = state.results?.has_revenue;
    $('drawer-risk-tag').textContent = d.risk_level;
    $('drawer-risk-tag').className = `risk-tag ${d.risk_level}`;
    animateNumber($('drawer-churn-prob'), d.churn_prob, { duration: 700, format: v => `${v.toFixed(1)}%` });
    $('drawer-prob-fill').style.setProperty('--w', `${Math.min(100, d.churn_prob)}%`);
    $('drawer-prob-fill').className = `drawer-prob-fill ${d.risk_level}`;
    $('drawer-loss').textContent = hasRev ? fmtMoney(d.expected_loss) : d.expected_loss.toFixed(2);
    $('drawer-revenue').textContent = hasRev ? fmtMoney(d.revenue) : '–';
    $('drawer-tenure').textContent = d.tenure != null ? `${Math.round(d.tenure)} months` : '–';
    renderImpacts('drawer-reasons', d.reasons, 6);
    $('drawer-recs').innerHTML = d.recommendations.map(r => `<li>${escapeHtml(r)}</li>`).join('');
    renderActivity(d.interventions);
    $('drawer-attrs').innerHTML = Object.entries(d.attrs).map(([k, v]) =>
      `<div><dt>${escapeHtml(humanize(k))}</dt><dd>${v == null ? '–' : escapeHtml(v)}</dd></div>`).join('');
    drawer.dataset.attrs = JSON.stringify(d.attrs);
    stagger(drawer.querySelectorAll('.drawer-row'), 10);
  } catch (err) {
    showToast(err.message, 'error');
    closeDrawer();
  }
}

export function closeDrawer() {
  const drawer = $('customer-drawer');
  if (!drawer.classList.contains('is-open')) return;
  setOverlay(drawer, false);
  currentId = null;
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  returnFocus = null;
}

export function initDrawer({ onSimulate }) {
  simulate = onSimulate;
  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-sim-btn').addEventListener('click', () => {
    const attrs = JSON.parse($('customer-drawer').dataset.attrs || '{}');
    const id = currentId;
    closeDrawer();
    simulate(id, attrs);
  });
  $('customer-drawer').addEventListener('click', e => { if (e.target.id === 'customer-drawer') closeDrawer(); });
  $('customer-drawer').addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDrawer(); return; }
    if (e.key !== 'Tab') return;
    const focusable = [...$('customer-drawer').querySelectorAll('button:not([disabled]), select, input, summary')].filter(el => el.offsetParent);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  $('log-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (!currentId) return;
    try {
      await post(`/customers/${encodeURIComponent(currentId)}/interventions`, { kind: $('log-kind').value, notes: $('log-notes').value });
      $('log-notes').value = '';
      const d = await get(`/customers/${encodeURIComponent(currentId)}`);
      renderActivity(d.interventions);
      showToast('Action logged.');
      emit('interventions');
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('drawer-activity').addEventListener('change', async e => {
    const sel = e.target.closest('.outcome-select');
    if (!sel) return;
    try {
      await patch(`/interventions/${sel.dataset.id}`, { outcome: sel.value });
      showToast('Outcome updated.');
      emit('interventions');
    } catch (err) { showToast(err.message, 'error'); }
  });
}
