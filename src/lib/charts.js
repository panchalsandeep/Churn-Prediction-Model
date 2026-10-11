/* Chart.js is loaded on demand (and tree-shaken to the controllers we use) so it
   stays out of the initial bundle. */

let ChartCtor = null;
let loading = null;

export function loadChart() {
  if (ChartCtor) return Promise.resolve(ChartCtor);
  loading ??= import('chart.js').then(m => {
    m.Chart.register(
      m.LineController, m.BarController, m.DoughnutController,
      m.LineElement, m.PointElement, m.BarElement, m.ArcElement,
      m.CategoryScale, m.LinearScale, m.Filler, m.Legend, m.Tooltip,
    );
    m.Chart.defaults.font.family = 'IBM Plex Sans';
    ChartCtor = m.Chart;
    return ChartCtor;
  });
  return loading;
}

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Colours that follow the active theme; read fresh each time a chart is built. */
export function chartTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    dark,
    text: css('--text-2'),
    title: css('--text-3'),
    grid: css('--chart-grid'),
    tooltipBg: dark ? 'rgba(20,33,39,0.97)' : 'rgba(250,247,240,0.97)',
    tooltipBorder: dark ? 'rgba(214,232,236,0.18)' : 'rgba(11,61,74,0.12)',
    tooltipTitle: css('--text-1'),
    track: dark ? 'rgba(255,255,255,0.09)' : 'rgba(11,61,74,0.08)',
    primary: dark ? '#7FD1CF' : '#0B3D4A',
    reference: dark ? 'rgba(255,255,255,0.22)' : 'rgba(11,61,74,0.22)',
  };
}
