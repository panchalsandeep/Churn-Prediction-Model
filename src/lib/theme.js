/* Light/dark theme with a persisted choice and a circular reveal transition. */
import { prefersReducedMotion } from './motion.js';

const KEY = 'churnsight-theme';
const root = document.documentElement;
const listeners = new Set();

export const getTheme = () => (root.dataset.theme === 'dark' ? 'dark' : 'light');

/** Subscribe to theme changes; callback runs synchronously after the attribute flips. */
export const onThemeChange = fn => { listeners.add(fn); return () => listeners.delete(fn); };

function apply(theme) {
  if (theme === 'dark') root.setAttribute('data-theme', 'dark');
  else root.removeAttribute('data-theme');
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', theme === 'dark' ? '#0C161A' : '#F3EFE6');
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.setAttribute('aria-pressed', String(theme === 'dark'));
    btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }
  listeners.forEach(fn => fn(theme));
}

export function setTheme(theme, origin) {
  if (theme === getTheme()) return;
  try { localStorage.setItem(KEY, theme); } catch { /* storage may be blocked */ }

  const canTransition = typeof document.startViewTransition === 'function' && !prefersReducedMotion();
  if (!canTransition) {
    root.classList.add('theme-fade');
    apply(theme);
    setTimeout(() => root.classList.remove('theme-fade'), 400);
    return;
  }

  const x = origin?.x ?? window.innerWidth - 40;
  const y = origin?.y ?? 34;
  const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
  const transition = document.startViewTransition(() => apply(theme));
  transition.ready.then(() => {
    root.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
      { duration: 600, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => {});
}

export function toggleTheme(origin) {
  setTheme(getTheme() === 'dark' ? 'light' : 'dark', origin);
}

export function initTheme() {
  apply(getTheme()); // sync button state with the attribute set by the inline bootstrap script
  document.getElementById('theme-toggle')?.addEventListener('click', e => {
    const r = e.currentTarget.getBoundingClientRect();
    toggleTheme({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
  });
  // Follow the OS only until the user makes an explicit choice.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
    let saved = null;
    try { saved = localStorage.getItem(KEY); } catch { /* ignore */ }
    if (!saved) apply(e.matches ? 'dark' : 'light');
  });
}
