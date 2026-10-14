/* Customers: server-side paging, sorting, filtering and search; bulk export; open the 360 drawer. */
import { $, qsa, state, escapeHtml, safeStorage, showToast, fmtMoney, fmtInt, on, replay } from '../lib/ctx.js';
import { get, download } from '../lib/api.js';
import { debounce, stagger, prefersReducedMotion } from '../lib/motion.js';
import { openCustomer } from './drawer.js';

export const table = {
  page: 1,
  limit: Number(safeStorage('churnsight-page-size')) || 20,
  sort: 'prob',
  dir: 'desc',
  tier: 'All',
  q: '',
  total: 0,
  pages: 1,
  selected: new Set(),
};

let controller = null;
let loaded = false;

const SKELETON_ROW = `<tr class="skeleton-row" aria-hidden="true">${'<td><span class="skeleton"></span></td>'.repeat(10)}</tr>`;

function params(extra = {}) {
  const p = new URLSearchParams({ sort: table.sort, dir: table.dir, ...extra });
  if (table.tier !== 'All') p.set('tier', table.tier);
  if (table.q) p.set('q', table.q);
  return p;
}

const deltaCell = d => (d == null ? '<span class="muted">–</span>'
  : `<span class="delta ${d > 0.5 ? 'up' : d < -0.5 ? 'down' : ''}">${d > 0 ? '+' : ''}${d.toFixed(1)}</span>`);

function rowHtml(c, hasRevenue) {
  const fill = c.risk_level === 'High' ? 'fill-high' : c.risk_level === 'Medium' ? 'fill-medium' : 'fill-low';
  const id = escapeHtml(c.id);
  const checked = table.selected.has(c.id);
  return `
    <tr class="customer-row${checked ? ' is-selected' : ''}" data-customer-id="${id}" tabindex="0" aria-selected="${checked}">
      <td class="col-select"><input type="checkbox" class="row-check" aria-label="Select ${id}" ${checked ? 'checked' : ''} /></td>
      <td><strong>${id}</strong></td>
      <td><span class="risk-tag ${escapeHtml(c.risk_level)}">${escapeHtml(c.risk_level)}</span></td>
      <td><div class="prob-bar-wrap"><div class="prob-mini-bar"><div class="prob-mini-fill ${fill}" style="width:${c.churn_prob}%"></div></div><span>${c.churn_prob}%</span></div></td>
      <td>${hasRevenue ? fmtMoney(c.expected_loss) : c.expected_loss.toFixed(1)}</td>
      <td>${hasRevenue ? fmtMoney(c.revenue) : '–'}</td>
      <td>${c.tenure != null ? `${Math.round(c.tenure)} mo` : '–'}</td>
      <td class="driver-cell">${c.top_driver ? escapeHtml(c.top_driver) : '<span class="muted">–</span>'}</td>
      <td>${deltaCell(c.delta)}</td>
      <td>${c.actual === 1 ? '<span class="churn-yes">● Churned</span>' : c.actual === 0 ? '<span class="churn-no">● Retained</span>' : '<span class="muted">–</span>'}</td>
    </tr>`;
}

export async function loadTable({ animate = true } = {}) {
  if (!state.results) return;
  controller?.abort();
  controller = new AbortController();
  const tbody = $('customer-tbody');
  if (!loaded) tbody.innerHTML = SKELETON_ROW.repeat(8);
  try {
    const res = await get(`/customers?${params({ page: table.page, limit: table.limit })}`, { signal: controller.signal });
    loaded = true;
    table.total = res.total;
    table.pages = res.pages;
    const start = (res.page - 1) * res.limit;
    $('customer-count').textContent = res.total
      ? `Showing ${fmtInt(start + 1)}–${fmtInt(start + res.customers.length)} of ${fmtInt(res.total)} customers`
      : 'Showing 0 customers';
    if (!res.customers.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty-row">No customers match the current filters.</td></tr>';
    } else {
      tbody.innerHTML = res.customers.map(c => rowHtml(c, state.results.has_revenue)).join('');
      if (animate) {
        const rows = tbody.querySelectorAll('.customer-row');
        stagger(rows, 16);
        rows.forEach(r => r.classList.add('row-in'));
      }
    }
    renderPagination();
    syncSortHeaders();
    syncSelectionUi();
  } catch (err) {
    if (err.name === 'AbortError') return;
    tbody.innerHTML = `<tr><td colspan="10" class="empty-row">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderPagination() {
  const wrap = $('table-pagination');
  const { pages, page } = table;
  if (pages <= 1) { wrap.innerHTML = ''; return; }
  const shown = new Set([1, pages, page - 1, page, page + 1].filter(n => n >= 1 && n <= pages));
  let html = `<button class="page-btn" ${page === 1 ? 'disabled' : ''} data-page="${page - 1}">‹ Prev</button>`;
  let prev = 0;
  [...shown].sort((a, b) => a - b).forEach(n => {
    if (n - prev > 1) html += '<span class="page-gap" aria-hidden="true">…</span>';
    html += `<button class="page-btn ${n === page ? 'active' : ''}" data-page="${n}" ${n === page ? 'aria-current="page"' : ''}>${n}</button>`;
    prev = n;
  });
  html += `<button class="page-btn" ${page === pages ? 'disabled' : ''} data-page="${page + 1}">Next ›</button>`;
  wrap.innerHTML = html;
}

function syncSortHeaders() {
  qsa('.sort-btn').forEach(btn => {
    const active = table.sort === btn.dataset.sort;
    btn.dataset.dir = active ? table.dir : '';
    btn.closest('th').setAttribute('aria-sort', active ? (table.dir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
}

function syncSelectionUi() {
  const bar = $('bulk-bar');
  const count = table.selected.size;
  $('bulk-count').textContent = `${count} selected`;
  if (count && bar.hidden) { bar.hidden = false; replay(bar, 'pop-in'); } else if (!count) bar.hidden = true;
  const checks = qsa('#customer-tbody .row-check');
  const on = checks.filter(c => c.checked).length;
  const all = $('select-all');
  all.checked = checks.length > 0 && on === checks.length;
  all.indeterminate = on > 0 && on < checks.length;
}

export function setTierFilter(tier) {
  table.tier = tier;
  table.page = 1;
  qsa('#risk-filters .risk-filter').forEach(b => b.classList.toggle('active', b.dataset.risk === tier));
  loadTable();
}

export function focusSearch() {
  $('customer-search').focus();
}

export function initCustomers() {
  $('page-size').value = String(table.limit);

  $('table-pagination').addEventListener('click', e => {
    const btn = e.target.closest('.page-btn[data-page]');
    if (!btn || btn.disabled) return;
    table.page = parseInt(btn.dataset.page, 10);
    loadTable();
    $('customer-table').scrollIntoView({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  });

  $('customer-table').querySelector('thead').addEventListener('click', e => {
    const btn = e.target.closest('.sort-btn');
    if (!btn) return;
    const key = btn.dataset.sort;
    if (table.sort !== key) { table.sort = key; table.dir = key === 'id' || key === 'driver' ? 'asc' : 'desc'; }
    else if (table.dir === 'desc') table.dir = 'asc';
    else { table.sort = 'prob'; table.dir = 'desc'; }       // third click returns to the default order
    table.page = 1;
    loadTable();
  });

  $('customer-tbody').addEventListener('change', e => {
    const check = e.target.closest('.row-check');
    if (!check) return;
    const row = check.closest('.customer-row');
    const id = row.dataset.customerId;
    if (check.checked) table.selected.add(id); else table.selected.delete(id);
    row.classList.toggle('is-selected', check.checked);
    row.setAttribute('aria-selected', String(check.checked));
    syncSelectionUi();
  });

  $('select-all').addEventListener('change', e => {
    qsa('#customer-tbody .customer-row').forEach(row => {
      const on = e.target.checked;
      if (on) table.selected.add(row.dataset.customerId); else table.selected.delete(row.dataset.customerId);
      row.classList.toggle('is-selected', on);
      row.setAttribute('aria-selected', String(on));
      row.querySelector('.row-check').checked = on;
    });
    syncSelectionUi();
  });

  $('bulk-clear').addEventListener('click', () => { table.selected.clear(); loadTable({ animate: false }); });
  $('bulk-export').addEventListener('click', async () => {
    try {
      await download('/customers/export', 'churnsight-selected.csv', { method: 'POST', json: { ids: [...table.selected] } });
      showToast(`Exported ${table.selected.size} customers.`);
    } catch (err) { showToast(err.message, 'error'); }
  });
  $('export-customers-btn').addEventListener('click', () => exportFiltered());

  $('customer-tbody').addEventListener('click', e => {
    if (e.target.closest('.col-select')) return;
    const row = e.target.closest('.customer-row');
    if (row) openCustomer(row.dataset.customerId, row);
  });
  $('customer-tbody').addEventListener('keydown', e => {
    if ((e.key !== 'Enter' && e.key !== ' ') || e.target.closest('.col-select')) return;
    const row = e.target.closest('.customer-row');
    if (row) { e.preventDefault(); openCustomer(row.dataset.customerId, row); }
  });

  qsa('#risk-filters .risk-filter').forEach(btn => btn.addEventListener('click', () => setTierFilter(btn.dataset.risk)));

  const applySearch = debounce(value => { table.q = value; table.page = 1; loadTable(); }, 220);
  $('customer-search').addEventListener('input', e => applySearch(e.target.value.trim()));

  $('page-size').addEventListener('change', e => {
    table.limit = Number(e.target.value) || 20;
    safeStorage('churnsight-page-size', String(table.limit));
    table.page = 1;
    loadTable();
  });

  on('results', r => {
    $('th-revenue').textContent = r.has_revenue ? 'Revenue / mo' : 'Revenue (n/a)';
    table.selected.clear();
    table.page = 1;
    loaded = false;
    if (state.currentSection === 'customers') loadTable();
  });
}

export async function exportFiltered() {
  if (!state.results) { showToast('Train a model before exporting.', 'error'); return; }
  try {
    await download(`/customers/export?${params()}`, `churnsight-risk-${new Date().toISOString().slice(0, 10)}.csv`);
    showToast('Export ready.');
  } catch (err) { showToast(err.message, 'error'); }
}

export async function exportAll() {
  if (!state.results) { showToast('Train a model before exporting.', 'error'); return; }
  try {
    await download('/customers/export?sort=loss&dir=desc', `churnsight-risk-${new Date().toISOString().slice(0, 10)}.csv`);
    showToast('Export ready.');
  } catch (err) { showToast(err.message, 'error'); }
}
