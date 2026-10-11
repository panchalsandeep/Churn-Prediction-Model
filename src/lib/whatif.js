/* What-if simulator: adjust a scored customer's levers and re-score live,
   plus the "what is driving this score" impact bars. */
import { debounce, stagger } from './motion.js';

const $ = id => document.getElementById(id);

const LEVERS = [
  { key: 'tenure_months',     label: 'Tenure',          unit: ' mo', min: 0,  max: 72,  step: 1 },
  { key: 'monthly_charges',   label: 'Monthly charges', unit: '',    min: 10, max: 150, step: 1, prefix: '$' },
  { key: 'num_products',      label: 'Products',        unit: '',    min: 1,  max: 8,   step: 1 },
  { key: 'num_logins_last30', label: 'Logins (30d)',    unit: '',    min: 0,  max: 60,  step: 1 },
  { key: 'support_tickets',   label: 'Support tickets', unit: '',    min: 0,  max: 15,  step: 1 },
  { key: 'last_login_days',   label: 'Days since login', unit: ' d', min: 0,  max: 90,  step: 1 },
];
const CONTRACTS = ['Month-to-month', 'One year', 'Two year'];

const FEATURE_LABELS = {
  age: 'Age', gender: 'Gender', location: 'Location', tenure_months: 'Tenure',
  monthly_charges: 'Monthly charges', total_charges: 'Total charges', num_products: 'Products',
  num_logins_last30: 'Logins (30d)', support_tickets: 'Support tickets',
  last_login_days: 'Days since login', contract_type: 'Contract', payment_method: 'Payment method',
};

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const fmtValue = v => (typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(1)) : v);

/** Diverging bars: red pushes churn risk up, green pulls it down. */
export function renderImpacts(impacts = []) {
  const box = $('impact-list');
  const top = impacts.filter(i => Math.abs(i.impact) >= 0.1).slice(0, 6);
  if (!top.length) {
    box.innerHTML = '<p class="impact-empty">No single factor moves this score noticeably.</p>';
    return;
  }
  const max = Math.max(...top.map(i => Math.abs(i.impact)), 1);
  box.innerHTML = top.map(i => {
    const up = i.impact > 0;
    const width = Math.max(4, (Math.abs(i.impact) / max) * 100);
    return `<div class="impact-row ${up ? 'up' : 'down'}">
      <span class="impact-name">${esc(FEATURE_LABELS[i.feature] || i.feature)}
        <small>${esc(fmtValue(i.value))}</small></span>
      <span class="impact-track"><span class="impact-fill" style="--w:${width}%"></span></span>
      <span class="impact-val">${up ? '+' : '−'}${Math.abs(i.impact).toFixed(1)} pts</span>
    </div>`;
  }).join('');
  stagger(box.querySelectorAll('.impact-row'), 8);
}

/**
 * @param {object} deps
 * @param {(payload:object, opts:{signal:AbortSignal}) => Promise<object>} deps.score  calls /api/predict with explain
 * @param {(result:object) => void} deps.render  paints gauge, badge and recommendations
 */
export function createWhatIf({ score, render }) {
  let base = null;      // payload + result captured when the user pressed Predict
  let current = null;   // payload being tweaked
  let controller = null;

  const delta = $('whatif-delta');

  function buildControls() {
    $('whatif-levers').innerHTML = LEVERS.map(l => `
      <label class="lever">
        <span class="lever-top"><span>${l.label}</span><output id="lv-out-${l.key}"></output></span>
        <input type="range" id="lv-${l.key}" data-key="${l.key}" min="${l.min}" max="${l.max}" step="${l.step}" />
      </label>`).join('');
    $('whatif-contract').innerHTML = CONTRACTS.map(c =>
      `<button type="button" role="radio" class="chip" data-contract="${c}">${c}</button>`).join('');
  }

  function syncControls() {
    LEVERS.forEach(l => {
      const input = $(`lv-${l.key}`);
      const value = current[l.key];
      // Widen the range if the form held a value beyond the slider's default max.
      input.max = Math.max(l.max, Math.ceil(value));
      input.value = value;
      $(`lv-out-${l.key}`).textContent = `${l.prefix || ''}${fmtValue(Number(value))}${l.unit}`;
      input.style.setProperty('--fill', `${((value - l.min) / (input.max - l.min)) * 100}%`);
    });
    $('whatif-contract').querySelectorAll('.chip').forEach(chip => {
      const on = chip.dataset.contract === current.contract_type;
      chip.classList.toggle('active', on);
      chip.setAttribute('aria-checked', String(on));
    });
  }

  function showDelta(result) {
    const diff = +(result.churn_probability - base.result.churn_probability).toFixed(1);
    delta.className = 'whatif-delta';
    if (Math.abs(diff) < 0.05) { delta.textContent = ''; return; }
    delta.classList.add(diff > 0 ? 'worse' : 'better');
    delta.textContent = `${diff > 0 ? '▲' : '▼'} ${Math.abs(diff)} pts vs original`;
  }

  const rescore = debounce(async () => {
    controller?.abort();
    controller = new AbortController();
    try {
      const result = await score(current, { signal: controller.signal });
      render(result);
      renderImpacts(result.impacts);
      showDelta(result);
    } catch (err) {
      if (err.name !== 'AbortError') console.warn('What-if scoring failed:', err);
    }
  }, 220);

  function change(key, value) {
    current = { ...current, [key]: value };
    if (key === 'tenure_months' || key === 'monthly_charges') {
      current.total_charges = +(current.tenure_months * current.monthly_charges).toFixed(2);
    }
    syncControls();
    rescore();
  }

  buildControls();
  $('whatif-levers').addEventListener('input', e => {
    const input = e.target.closest('input[data-key]');
    if (input) change(input.dataset.key, Number(input.value));
  });
  $('whatif-contract').addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (chip) change('contract_type', chip.dataset.contract);
  });
  $('whatif-reset').addEventListener('click', () => {
    if (!base) return;
    rescore.cancel();
    controller?.abort();
    current = { ...base.payload };
    syncControls();
    render(base.result);
    renderImpacts(base.result.impacts);
    delta.textContent = '';
    delta.className = 'whatif-delta';
  });

  return {
    /** Call after the main form produces a prediction. */
    start(payload, result) {
      base = { payload: { ...payload }, result };
      current = { ...payload };
      rescore.cancel();
      controller?.abort();
      syncControls();
      renderImpacts(result.impacts);
      delta.textContent = '';
      delta.className = 'whatif-delta';
    },
  };
}
