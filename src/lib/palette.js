/* Command palette (Ctrl/Cmd+K), keyboard shortcuts and the shortcuts help dialog. */
import { stagger } from './motion.js';

const $ = id => document.getElementById(id);
const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

/** Higher is better; 0 means no match. Substring beats an in-order subsequence. */
function score(query, text) {
  if (!query) return 1;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const at = t.indexOf(q);
  if (at !== -1) return 100 - at - (t.length - q.length) * 0.1;
  let ti = 0;
  for (const ch of q) {
    ti = t.indexOf(ch, ti);
    if (ti === -1) return 0;
    ti++;
  }
  return 10;
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = text => text.replace(/[&<>"']/g, c => ESC[c]);

function highlight(text, query) {
  const at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at === -1) return esc(text);
  const end = at + query.length;
  return `${esc(text.slice(0, at))}<mark>${esc(text.slice(at, end))}</mark>${esc(text.slice(end))}`;
}

/**
 * @param {object} api
 * @param {() => Array<{id:string,label:string,group:string,hint?:string,run:()=>void}>} api.getCommands
 * @param {(q:string) => Array<{id:string,risk_level:string,churn_prob:number}>} api.findCustomers
 * @param {(id:string) => void} api.openCustomer
 * @param {(section:string) => void} api.navigate
 * @param {() => void} api.focusSearch
 * @param {() => void} api.toggleTheme
 */
export function initPalette(api) {
  const overlay = $('palette');
  const input = $('palette-input');
  const list = $('palette-list');
  const shortcuts = $('shortcuts');
  let items = [];
  let active = 0;
  let returnFocus = null;

  $('palette-kbd').textContent = isMac ? '⌘ K' : 'Ctrl K';

  const openOverlay = el => {
    returnFocus = document.activeElement;
    el.classList.add('is-open');
    el.setAttribute('aria-hidden', 'false');
  };
  const closeOverlay = el => {
    if (!el.classList.contains('is-open')) return;
    el.classList.remove('is-open');
    el.setAttribute('aria-hidden', 'true');
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  };
  const anyOpen = () => overlay.classList.contains('is-open') || shortcuts.classList.contains('is-open');

  let buildToken = 0;

  function build(query) {
    const token = ++buildToken;
    const commands = api.getCommands()
      .map(c => ({ ...c, s: score(query, `${c.label} ${c.keywords || ''}`) }))
      .filter(c => c.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 9);
    items = commands;
    active = 0;
    render(query);

    // Customers are searched on the server; results arrive after the commands are already on screen.
    if (query.length >= 2) {
      Promise.resolve(api.findCustomers(query)).then(found => {
        if (token !== buildToken || !found?.length) return;
        items = [...commands, ...found.slice(0, 6).map(c => ({
          id: `customer:${c.id}`, label: c.id, group: 'Customers',
          hint: `${c.risk_level} risk · ${c.churn_prob}%`, run: () => api.openCustomer(c.id),
        }))];
        render(query);
      }).catch(() => {});
    }
  }

  function render(query) {
    if (!items.length) {
      list.innerHTML = '<li class="palette-empty" role="presentation">No matches. Try a page name or a customer ID.</li>';
      input.removeAttribute('aria-activedescendant');
      return;
    }
    let lastGroup = '';
    list.innerHTML = items.map((item, i) => {
      const head = item.group !== lastGroup ? `<li class="palette-group" role="presentation">${item.group}</li>` : '';
      lastGroup = item.group;
      return `${head}<li class="palette-item${i === active ? ' is-active' : ''}" role="option" id="pal-${i}" data-index="${i}" aria-selected="${i === active}">
        <span class="palette-label">${highlight(item.label, query)}</span>
        ${item.hint ? `<span class="palette-hint">${highlight(item.hint, '')}</span>` : ''}
      </li>`;
    }).join('');
    stagger(list.querySelectorAll('.palette-item'), 8);
    input.setAttribute('aria-activedescendant', `pal-${active}`);
  }

  function setActive(next) {
    if (!items.length) return;
    active = (next + items.length) % items.length;
    list.querySelectorAll('.palette-item').forEach(node => {
      const on = Number(node.dataset.index) === active;
      node.classList.toggle('is-active', on);
      node.setAttribute('aria-selected', String(on));
      if (on) node.scrollIntoView({ block: 'nearest' });
    });
    input.setAttribute('aria-activedescendant', `pal-${active}`);
  }

  function run(index) {
    const item = items[index];
    if (!item) return;
    closeOverlay(overlay);
    returnFocus = null; // the command decides where focus goes
    item.run();
  }

  const openPalette = () => {
    closeOverlay(shortcuts);
    openOverlay(overlay);
    input.value = '';
    build('');
    input.focus(); // focus() forces a style recalc, so the just-opened overlay is already focusable
  };
  const openShortcuts = () => {
    closeOverlay(overlay);
    openOverlay(shortcuts);
    shortcuts.querySelector('.palette-panel').focus?.();
  };

  input.addEventListener('input', () => build(input.value.trim()));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); run(active); }
    else if (e.key === 'Tab') { e.preventDefault(); }
  });
  list.addEventListener('click', e => {
    const node = e.target.closest('.palette-item');
    if (node) run(Number(node.dataset.index));
  });
  list.addEventListener('mousemove', e => {
    const node = e.target.closest('.palette-item');
    if (node && Number(node.dataset.index) !== active) setActive(Number(node.dataset.index));
  });
  [overlay, shortcuts].forEach(el => el.addEventListener('mousedown', e => {
    if (e.target === el) closeOverlay(el);
  }));
  $('palette-trigger')?.addEventListener('click', openPalette);

  /* Global shortcuts */
  const NAV_KEYS = { u: 'upload', o: 'overview', h: 'health', c: 'cohorts', a: 'alerts', n: 'analytics', t: 'customers', p: 'predict' };
  let chordUntil = 0;

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      overlay.classList.contains('is-open') ? closeOverlay(overlay) : openPalette();
      return;
    }
    if (e.key === 'Escape' && anyOpen()) {
      closeOverlay(overlay);
      closeOverlay(shortcuts);
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const el = e.target;
    const typing = el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
    if (typing || anyOpen()) return;

    const key = e.key.toLowerCase();
    if (e.key === '?') { e.preventDefault(); openShortcuts(); return; }
    if (e.key === '/') { e.preventDefault(); api.focusSearch(); return; }
    if (key === 'd') { api.toggleTheme(); return; }
    if (key === 'g') { chordUntil = performance.now() + 1200; return; }
    if (performance.now() < chordUntil && NAV_KEYS[key]) {
      chordUntil = 0;
      api.navigate(NAV_KEYS[key]);
    }
  });

  return { open: openPalette, openShortcuts };
}
