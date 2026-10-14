/* In-app dialogs that replace the browser's confirm() and prompt(): themed, keyboard-friendly, focus-trapped, promise-based. */
import { escapeHtml } from './ctx.js';

const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function open({ title, body, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false, input = null, copy = null }) {
  return new Promise(resolve => {
    const previous = document.activeElement;
    const root = document.createElement('div');
    root.className = 'dlg-root';
    root.setAttribute('data-no-capture', '');
    root.innerHTML = `<div class="dlg-backdrop"></div>
      <div class="dlg" role="alertdialog" aria-modal="true" aria-labelledby="dlg-title" aria-describedby="dlg-body">
        <h3 id="dlg-title">${escapeHtml(title)}</h3>
        <div id="dlg-body" class="dlg-text">${body}</div>
        ${copy ? `<div class="dlg-copy"><code>${escapeHtml(copy)}</code><button type="button" class="btn-secondary btn-small" data-copy>Copy</button></div>` : ''}
        ${input ? `<input class="dlg-input" type="text" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(input.placeholder || '')}" aria-label="${escapeHtml(input.label || 'Confirmation')}" />` : ''}
        <div class="dlg-actions">
          ${cancelLabel ? `<button type="button" class="btn-secondary btn-small" data-cancel>${escapeHtml(cancelLabel)}</button>` : ''}
          <button type="button" class="${danger ? 'btn-danger' : 'btn-primary'} btn-small" data-ok ${input ? 'disabled' : ''}>${escapeHtml(confirmLabel)}</button>
        </div></div>`;
    document.body.appendChild(root);
    const box = root.querySelector('.dlg');
    const ok = root.querySelector('[data-ok]');
    const field = root.querySelector('.dlg-input');
    setTimeout(() => root.classList.add('show'), 20);          // not rAF: it pauses in background tabs and the dialog would stay see-through
    (field || ok).focus();

    const done = value => {
      document.removeEventListener('keydown', onKey, true);
      root.classList.remove('show');
      setTimeout(() => { root.remove(); previous?.focus?.(); }, reduced() ? 0 : 220);
      resolve(value);
    };
    const accept = () => { if (!ok.disabled) done(field ? field.value : true); };
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); done(field ? null : false); }
      else if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); accept(); }
      else if (e.key === 'Tab') {                                  // keep focus inside the dialog
        const items = [...box.querySelectorAll('button:not([disabled]), input')];
        const first = items[0]; const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    ok.addEventListener('click', accept);
    root.querySelector('[data-cancel]')?.addEventListener('click', () => done(field ? null : false));
    root.querySelector('.dlg-backdrop').addEventListener('click', () => done(field ? null : false));
    root.querySelector('[data-copy]')?.addEventListener('click', async e => {
      try { await navigator.clipboard.writeText(copy); e.target.textContent = 'Copied'; } catch { e.target.textContent = 'Select and copy'; }
    });
    if (field && input.match) field.addEventListener('input', () => { ok.disabled = field.value.trim() !== input.match; });
  });
}

/** Resolves true or false. Use `danger` for actions that cannot be undone. */
export const confirmDialog = (message, { title = 'Are you sure?', confirmLabel = 'Confirm', danger = false } = {}) =>
  open({ title, body: `<p>${escapeHtml(message)}</p>`, confirmLabel, danger });

/** The user must type `match` exactly to enable the button. Resolves the typed text, or null if cancelled. */
export const typeToConfirm = (message, match, { title = 'Type to confirm', confirmLabel = 'Delete', danger = true } = {}) =>
  open({ title, body: `<p>${escapeHtml(message)}</p><p class="muted">Type <b>${escapeHtml(match)}</b> to continue.</p>`, confirmLabel, danger, input: { match, label: `Type ${match}` } });

/** Show a value that will not be shown again, with a copy button. */
export const revealSecret = (message, secret, { title = 'Copy this now' } = {}) =>
  open({ title, body: `<p>${escapeHtml(message)}</p>`, confirmLabel: 'Done', cancelLabel: '', copy: secret });
