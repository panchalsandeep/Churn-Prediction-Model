/* Chart plumbing: lazy Chart.js, per-section builders, theme-aware options, rebuild on theme change. */
import { loadChart, chartTheme } from './charts.js';
import { prefersReducedMotion } from './motion.js';
import { onThemeChange } from './theme.js';
import { $, showToast, state } from './ctx.js';

const builders = {};              // section -> [() => void]
const charts = {};                // key -> Chart
const dirty = new Set();
let Chart = null;
let animate = true;

export const registerCharts = (section, fn) => { (builders[section] ??= []).push(fn); };

export function destroyChart(key) {
  if (charts[key]) { charts[key].destroy(); delete charts[key]; }
}
export function destroyAll() { Object.keys(charts).forEach(destroyChart); }

export const markDirty = (...sections) => sections.forEach(s => dirty.add(s));
export const markAllDirty = () => markDirty(...Object.keys(builders));

export function chartAnimation(duration = 900) {
  return animate && !prefersReducedMotion() ? { duration, easing: 'easeOutQuart' } : false;
}

/** Build a section's charts the first time it is shown (and again whenever data/theme made it dirty). */
export async function renderSectionCharts(section, { animate: anim = true } = {}) {
  if (!dirty.has(section) || !state.results || !builders[section]) return;
  dirty.delete(section);
  try {
    Chart = await loadChart();
  } catch {
    dirty.add(section);
    showToast('Could not load the charting library. Check your connection.', 'error');
    return;
  }
  animate = anim;
  builders[section].forEach(build => build());
}

/** Create (or replace) a chart. `make` receives (theme) and returns a Chart.js config. */
export function makeChart(key, canvasId, make) {
  destroyChart(key);
  const canvas = $(canvasId);
  if (!canvas || !Chart) return null;
  charts[key] = new Chart(canvas.getContext('2d'), make(chartTheme()));
  return charts[key];
}

export function tooltipStyle() {
  const t = chartTheme();
  return {
    backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1,
    titleColor: t.tooltipTitle, bodyColor: t.text,
    titleFont: { weight: '700' }, bodyFont: { size: 12 }, padding: 10, cornerRadius: 8,
  };
}

export function gridStyle() {
  const t = chartTheme();
  return { grid: { color: t.grid }, ticks: { color: t.text, font: { size: 11 } }, border: { color: 'transparent' } };
}

export const axisTitle = text => ({ display: true, text, color: chartTheme().title });

export function legend(position = 'top') {
  return { position, labels: { usePointStyle: true, pointStyleWidth: 10, padding: 16, color: chartTheme().text } };
}

/** Rebuild what is on screen when the theme flips; other sections rebuild on their next visit. */
export function rebuildOnThemeChange(getSection, extra) {
  onThemeChange(() => {
    extra?.();
    if (!state.results) return;
    destroyAll();
    markAllDirty();
    renderSectionCharts(getSection(), { animate: false });
  });
}

export { chartTheme };
