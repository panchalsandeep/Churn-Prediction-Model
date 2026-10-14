/* Account health: at-risk accounts ranked by expected loss, with their main driver. */
import { $, qsa, state, escapeHtml, fmtMoney, fmtCompactMoney, fmtInt, on, replay } from '../lib/ctx.js';
import { get } from '../lib/api.js';
import { animateNumber } from '../lib/motion.js';
import { openCustomer } from './drawer.js';

const PAGE = 12;
let tier = 'All';
let shown = 0;
let total = 0;
let loading = false;

const card = (c, i, hasRevenue) => {
  const health = Math.round(100 - c.churn_prob);
  const d = c.delta;
  return `
    <div class="health-card card-in" style="--i:${i % 12}" data-customer-id="${escapeHtml(c.id)}" tabindex="0" role="button" aria-label="Open ${escapeHtml(c.id)}">
      <div class="health-card-header"><h4>${escapeHtml(c.id)}</h4><span class="health-tier ${escapeHtml(c.risk_level)}">${escapeHtml(c.risk_level)} risk</span></div>
      <div class="health-score" data-to="${health}">${health}</div>
      <div class="health-sub">health score · ${c.churn_prob}% churn probability${d != null && Math.abs(d) >= 0.5 ? ` · <span class="delta ${d > 0 ? 'up' : 'down'}">${d > 0 ? '+' : ''}${d.toFixed(1)} vs last run</span>` : ''}</div>
      <div class="health-bar"><div class="health-bar-fill ${escapeHtml(c.risk_level)}" style="width:${health}%"></div></div>
      <div class="health-facts">
        <div><span>Expected loss</span><b>${hasRevenue ? fmtCompactMoney(c.expected_loss) : c.expected_loss.toFixed(1)}</b></div>
        <div><span>Main driver</span><b>${c.top_driver ? escapeHtml(c.top_driver) : '–'}</b></div>
      </div>
    </div>`;
};

async function load(reset) {
  if (loading || !state.results) return;
  loading = true;
  const grid = $('health-grid');
  if (reset) { shown = 0; grid.innerHTML = '<div class="health-card empty-card"><div class="skeleton" style="width:60%"></div></div>'; }
  try {
    const p = new URLSearchParams({ sort: 'loss', dir: 'desc', limit: PAGE, page: Math.floor(shown / PAGE) + 1 });
    if (tier !== 'All') p.set('tier', tier);
    const res = await get(`/customers?${p}`);
    total = res.total;
    if (reset) grid.innerHTML = '';
    if (!res.customers.length && reset) {
      grid.innerHTML = '<div class="health-card empty-card"><p>No accounts in this tier.</p></div>';
    } else {
      grid.insertAdjacentHTML('beforeend', res.customers.map((c, i) => card(c, i, state.results.has_revenue)).join(''));
      grid.querySelectorAll('.health-score:not(.counted)').forEach(el => {
        el.classList.add('counted');
        animateNumber(el, +el.dataset.to, { duration: 800 });
      });
      shown += res.customers.length;
    }
    $('health-more').hidden = shown >= total;
    $('health-more').textContent = `Show more accounts (${fmtInt(total - shown)} left)`;
  } finally {
    loading = false;
  }
}

export function initHealth() {
  qsa('#health-risk-filters .risk-filter').forEach(btn => btn.addEventListener('click', () => {
    qsa('#health-risk-filters .risk-filter').forEach(b => b.classList.toggle('active', b === btn));
    tier = btn.dataset.filter;
    load(true);
  }));
  $('health-more').addEventListener('click', () => load(false));
  const open = e => {
    const el = e.target.closest('.health-card[data-customer-id]');
    if (el) openCustomer(el.dataset.customerId, el);
  };
  $('health-grid').addEventListener('click', open);
  $('health-grid').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); } });

  on('results', r => {
    const s = r.summary;
    animateNumber($('health-avg-score'), 100 - s.avg_risk, { duration: 800, format: v => v.toFixed(0) });
    animateNumber($('health-critical-count'), s.high_risk, { duration: 800 });
    animateNumber($('health-actionable-count'), s.actionable_customers, { duration: 800 });
    $('health-net-value').textContent = r.has_revenue ? fmtCompactMoney(s.expected_net_value) : '–';
    replay($('health-grid'), 'pop-in');
    load(true);
  });
}
