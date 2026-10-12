/* Small animation helpers shared across the UI. All of them degrade to an
   instant update when the user prefers reduced motion. */

const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
export const prefersReducedMotion = () => mq.matches;

export const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
export const easeInOutCubic = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/** Run `onFrame(easedProgress)` over `duration` ms. Returns a cancel function. */
export function tween(duration, onFrame, ease = easeOutCubic) {
  if (prefersReducedMotion() || duration <= 0) {
    onFrame(1);
    return () => {};
  }
  let raf = 0;
  const start = performance.now();
  const step = now => {
    const t = Math.min((now - start) / duration, 1);
    onFrame(ease(t));
    if (t < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

/** Count an element's text from `from` to `to`. A newer call on the same element wins. */
export function animateNumber(el, to, { from = 0, duration = 900, format = v => Math.round(v) } = {}) {
  if (!el) return;
  if (el._cancelCount) el._cancelCount();
  el._cancelCount = tween(duration, t => {
    el.textContent = format(from + (to - from) * t);
  });
}

/** Re-trigger a CSS animation class on a node. */
export function replay(node, className) {
  node.classList.remove(className);
  void node.offsetWidth;
  node.classList.add(className);
}

/** Give each node a --i index so CSS can stagger it (capped to keep long lists snappy). */
export function stagger(nodes, cap = 14) {
  Array.from(nodes).forEach((node, i) => node.style.setProperty('--i', String(Math.min(i, cap))));
}

export function debounce(fn, wait = 200) {
  let timer = 0;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}
