/* Shared state, DOM helpers and tiny UI primitives used by every view. */
import { replay } from './motion.js';

export const state = {
  results: null,         // /api/results payload (never contains the full customer list)
  user: null,            // Firebase user
  idToken: null,
  admin: { isAdmin: false },                 // platform administrator (Firebase user management)
  me: { role: 'owner', uid: 'local-dev', email: 'local-dev', workspace: { id: 'local', name: 'Local workspace' }, seats: 1 },
  currentSection: 'upload',
};

const RANK = { viewer: 0, analyst: 1, admin: 2, owner: 3 };
/** Does the signed-in user hold at least this role? (The server enforces it; this only decides what to show.) */
export const can = minimum => (RANK[state.me.role] ?? -1) >= RANK[minimum];

export const $ = id => document.getElementById(id);
export const qs = s => document.querySelector(s);
export const qsa = s => Array.from(document.querySelectorAll(s));

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ESC[c]);

export function safeStorage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage may be blocked */ }
  return null;
}

/* ── Formatting ─────────────────────────────────────────────── */
const money0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
export const fmtMoney = n => (n == null || Number.isNaN(+n) ? '–' : money0.format(+n));
export const fmtCompactMoney = n => (n == null || Number.isNaN(+n) ? '–' : `$${compact.format(+n)}`);
export const fmtInt = n => (n == null || Number.isNaN(+n) ? '–' : Math.round(+n).toLocaleString('en-US'));
export const fmtPct = (n, d = 1) => (n == null || Number.isNaN(+n) ? '–' : `${(+n).toFixed(d)}%`);

export function timeAgo(ts) {
  if (!ts) return '–';
  const s = Math.max(1, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return 'just now';
  const units = [[86400, 'd'], [3600, 'h'], [60, 'm']];
  for (const [secs, label] of units) if (s >= secs) return `${Math.floor(s / secs)}${label} ago`;
  return `${s}s ago`;
}

// "MonthlyCharges" and "monthly_charges" both read as "monthly charges".
export const humanize = name => String(name).replace(/_age_days$/, ' (days)').replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/_/g, ' ').toLowerCase();

/* ── Overlays, toasts ───────────────────────────────────────── */
export function setOverlay(el, open) {
  el.classList.toggle('is-open', open);
  el.setAttribute('aria-hidden', String(!open));
}

let toastTimer = 0;
export function showToast(msg, type = 'success') {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast ${type} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), type === 'error' ? 5000 : 3500);
}

/* ── Event bus: views react to new results without importing each other ── */
export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, e => fn(e.detail));

export function setBusy(button, busy, busyHtml) {
  if (busy) {
    button.dataset.html = button.innerHTML;
    button.disabled = true;
    button.innerHTML = busyHtml || '<span class="spinner-inline"></span> Working…';
  } else {
    button.disabled = false;
    if (button.dataset.html) button.innerHTML = button.dataset.html;
  }
}

export { replay };
