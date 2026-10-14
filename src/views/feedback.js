/* Bug report: click the bug icon, the page edges flash like a camera, a card slides in with the screenshot and a box for the message. */
import { $, escapeHtml, showToast } from '../lib/ctx.js';
import { post, recentRequestIds } from '../lib/api.js';

const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const wait = ms => new Promise(r => setTimeout(r, ms));
let busy = false;
let shot = null;       // data URL of the captured page

async function capture() {
  const { default: html2canvas } = await import('html2canvas-pro');
  const canvas = await html2canvas(document.body, {
    backgroundColor: getComputedStyle(document.body).backgroundColor || '#fff', logging: false, useCORS: true,
    scale: Math.min(window.devicePixelRatio || 1, 1.5), width: window.innerWidth, height: window.innerHeight, x: window.scrollX, y: window.scrollY,
    windowWidth: document.documentElement.clientWidth, windowHeight: window.innerHeight,
    ignoreElements: el => el.hasAttribute?.('data-no-capture'),
  });
  const max = 1600;
  const scale = Math.min(1, max / canvas.width);
  if (scale === 1) return canvas.toDataURL('image/jpeg', 0.72);
  const small = document.createElement('canvas');
  small.width = Math.round(canvas.width * scale);
  small.height = Math.round(canvas.height * scale);
  small.getContext('2d').drawImage(canvas, 0, 0, small.width, small.height);
  return small.toDataURL('image/jpeg', 0.72);
}

function closeCard() {
  const card = $('bug-card');
  card.classList.remove('open');
  $('bug-backdrop').classList.remove('open');
  setTimeout(() => { card.hidden = true; $('bug-backdrop').hidden = true; }, reduced() ? 0 : 320);
  $('bug-btn').focus();
}

function openCard(image, failed) {
  const card = $('bug-card');
  shot = image;
  $('bug-preview').innerHTML = image ? `<img src="${image}" alt="Screenshot of the page you are reporting" />` : '<p class="muted">The screenshot could not be taken, but you can still send your message.</p>';
  $('bug-include').checked = !!image;
  $('bug-include-row').hidden = !image;
  $('bug-msg').value = '';
  $('bug-state').textContent = failed ? 'Screenshot unavailable in this browser.' : '';
  $('bug-send').disabled = false;
  $('bug-body').hidden = false;
  $('bug-done').hidden = true;
  card.hidden = false;
  $('bug-backdrop').hidden = false;
  setTimeout(() => { card.classList.add('open'); $('bug-backdrop').classList.add('open'); }, 30);
  setTimeout(() => $('bug-msg').focus(), reduced() ? 0 : 350);
}

async function start() {
  if (busy) return;
  busy = true;
  const btn = $('bug-btn');
  btn.classList.add('busy');
  const frame = $('capture-frame');
  let image = null; let failed = false;
  const grab = capture().then(i => { image = i; }).catch(err => { failed = true; console.warn('Screenshot failed:', err); });
  if (!reduced()) {
    frame.classList.remove('flash');
    void frame.offsetWidth;
    frame.classList.add('flash');
  }
  await Promise.all([grab, wait(reduced() ? 0 : 520)]);
  frame.classList.remove('flash');
  btn.classList.remove('busy');
  busy = false;
  openCard(image, failed);
}

export function initFeedback() {
  $('bug-btn').addEventListener('click', start);
  $('bug-cancel').addEventListener('click', closeCard);
  $('bug-close').addEventListener('click', closeCard);
  $('bug-backdrop').addEventListener('click', closeCard);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('bug-card').hidden) closeCard(); });
  $('bug-form').addEventListener('submit', async e => {
    e.preventDefault();
    const message = $('bug-msg').value.trim();
    if (message.length < 3) { $('bug-state').textContent = 'Please describe what happened.'; $('bug-msg').focus(); return; }
    $('bug-send').disabled = true;
    $('bug-state').textContent = 'Sending…';
    try {
      const r = await post('/feedback', {
        message, screenshot: $('bug-include').checked ? shot : null, page: location.hash || location.pathname, viewport: `${innerWidth}x${innerHeight}`,
        agent: navigator.userAgent, app_version: document.documentElement.dataset.version || '', request_ids: recentRequestIds(),
      });
      $('bug-body').hidden = true;
      $('bug-done').hidden = false;
      $('bug-ref').textContent = r.id;
      setTimeout(() => { if (!$('bug-card').hidden) closeCard(); }, 2600);
    } catch (err) {
      $('bug-send').disabled = false;
      $('bug-state').textContent = '';
      showToast(err.message, 'error');
    }
  });
}
