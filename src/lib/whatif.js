/* What-if simulator: adjust a scored customer's levers and re-score live,
   plus the "what is driving this score" impact bars. Levers are built from the model's own features. */
import { debounce, stagger } from './motion.js';
import { $, escapeHtml, humanize } from './ctx.js';

const fmtValue = v => {
  if (typeof v !== 'number') return v;
  if (Number.isInteger(v)) return v;
  return Math.abs(v) >= 100 ? Math.round(v) : +v.toFixed(2);
};

/** Diverging bars: red pushes churn risk up, green pulls it down. `max` caps how many rows are shown. */
export function renderImpacts(target, impacts = [], max = 6) {
  const box = typeof target === 'string' ? $(target) : target;
  const top = impacts.filter(i => Math.abs(i.impact) >= 0.1).slice(0, max);
  if (!top.length) {
    box.innerHTML = '<p class="impact-empty">No single factor moves this score noticeably.</p>';
    return;
  }
  const peak = Math.max(...top.map(i => Math.abs(i.impact)), 1);
  box.innerHTML = top.map(i => {
    const up = i.impact > 0;
    const width = Math.max(4, (Math.abs(i.impact) / peak) * 100);
    return `<div class="impact-row ${up ? 'up' : 'down'}">
      <span class="impact-name">${escapeHtml(i.label || humanize(i.feature))}
        <small>${escapeHtml(fmtValue(i.value) ?? 'missing')}</small></span>
      <span class="impact-track"><span class="impact-fill" style="--w:${width}%"></span></span>
      <span class="impact-val">${up ? '+' : '−'}${Math.abs(i.impact).toFixed(1)} pts</span>
    </div>`;
  }).join('');
  stagger(box.querySelectorAll('.impact-row'), 8);
}

/**
 * @param {object} deps
 * @param {(values:object, opts:{signal:AbortSignal}) => Promise<object>} deps.score  calls /api/predict with explain
 * @param {(result:object) => void} deps.render  paints gauge, badge and recommendations
 */
export function createWhatIf({ score, render }) {
  let base = null;       // values + result captured when the user pressed Predict
  let current = null;
  let levers = [];
  let correlated = [];
  let schemaCorrelated = [];
  let fieldDefs = [];
  let chipField = null;
  let controller = null;
  const delta = $('whatif-delta');

  function pickLevers(fields) {
    const ranked = [...fields].sort((a, b) => a.rank - b.rank);
    correlated = schemaCorrelated;
    levers = ranked.filter(f => f.kind === 'number' && f.max > f.min && f.actionable !== false).slice(0, 6);   // only things someone could change
    chipField = ranked.find(f => f.kind === 'category' && f.options.length >= 2 && f.options.length <= 5) || null;
  }

  function buildControls() {
    $('whatif-levers').innerHTML = levers.map(l => `
      <label class="lever">
        <span class="lever-top"><span>${escapeHtml(l.label)}</span><output id="lv-out-${l.name}"></output></span>
        <input type="range" id="lv-${l.name}" data-key="${l.name}" min="${l.min}" max="${l.max}"
               step="${l.integer ? 1 : ((l.max - l.min) / 100).toPrecision(2)}" />
      </label>`).join('');
    $('whatif-contract').innerHTML = chipField
      ? chipField.options.map(c => `<button type="button" role="radio" class="chip" data-value="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join('')
      : '';
    $('whatif-contract').setAttribute('aria-label', chipField ? chipField.label : 'Category');
  }

  function syncControls() {
    levers.forEach(l => {
      const input = $(`lv-${l.name}`);
      const value = Number(current[l.name]);
      input.min = Math.min(l.min, value);
      input.max = Math.max(l.max, value);
      input.value = value;
      $(`lv-out-${l.name}`).textContent = fmtValue(value);
      input.style.setProperty('--fill', `${((value - input.min) / (input.max - input.min || 1)) * 100}%`);
    });
    if (chipField) {
      $('whatif-contract').querySelectorAll('.chip').forEach(chip => {
        const on = chip.dataset.value === String(current[chipField.name]);
        chip.classList.toggle('active', on);
        chip.setAttribute('aria-checked', String(on));
      });
    }
  }

  /** Warn when a scenario pulls apart features that normally move together: the model has rarely seen that, so the answer is less reliable. */
  function consistencyNote() {
    const moved = new Set(levers.filter(l => Math.abs(Number(current[l.name]) - Number(base.values[l.name])) > 0.15 * (l.max - l.min)).map(l => l.name));
    const pairs = correlated.filter(([a, b]) => (moved.has(a) !== moved.has(b)) && fieldDefs.some(f => f.name === a) && fieldDefs.some(f => f.name === b));
    const note = $('whatif-note');
    if (!note) return;
    note.hidden = !pairs.length;
    note.textContent = pairs.length ? `Heads up: ${pairs.slice(0, 2).map(([a, b]) => `${humanize(a)} and ${humanize(b)}`).join('; ')} usually move together, but here only one changed. The score is less reliable for a combination like that.` : '';
  }

  function showDelta(result) {
    const diff = +(result.churn_probability - base.result.churn_probability).toFixed(1);
    delta.className = 'whatif-delta';
    if (Math.abs(diff) < 0.05) { delta.textContent = ''; return; }
    delta.classList.add(diff > 0 ? 'worse' : 'better');
    delta.textContent = `${diff > 0 ? '▲' : '▼'} ${Math.abs(diff)} pts vs original`;
  }

  // While dragging only the probability is re-scored (cheap); the full explanation follows when the slider is released.
  const run = async explain => {
    controller?.abort();
    controller = new AbortController();
    try {
      const result = await score(current, { signal: controller.signal, explain });
      render(result);
      if (explain) renderImpacts('impact-list', result.impacts);
      showDelta(result);
    } catch (err) {
      if (err.name !== 'AbortError') console.warn('What-if scoring failed:', err);
    }
  };
  const rescore = debounce(() => run(false), 150);
  const explainSoon = debounce(() => run(true), 350);

  function change(key, value) {
    current = { ...current, [key]: value };
    syncControls();
    consistencyNote();
    rescore();
    explainSoon();
  }

  $('whatif-levers').addEventListener('input', e => {
    const input = e.target.closest('input[data-key]');
    if (input) change(input.dataset.key, Number(input.value));
  });
  $('whatif-contract').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (chip && chipField) change(chipField.name, chip.dataset.value);
  });
  $('whatif-reset').addEventListener('click', () => {
    if (!base) return;
    rescore.cancel();
    explainSoon.cancel();
    controller?.abort();
    current = { ...base.values };
    syncControls();
    render(base.result);
    renderImpacts('impact-list', base.result.impacts);
    delta.textContent = '';
    delta.className = 'whatif-delta';
  });

  return {
    /** Call after the main form produces a prediction. `fields` is the model's form schema. */
    start(values, result, fields, corr = []) {
      schemaCorrelated = corr;
      fieldDefs = fields;
      pickLevers(fields);
      buildControls();
      base = { values: { ...values }, result };
      current = { ...values };
      rescore.cancel();
      explainSoon.cancel();
      controller?.abort();
      syncControls();
      renderImpacts('impact-list', result.impacts);
      delta.textContent = '';
      delta.className = 'whatif-delta';
    },
  };
}
