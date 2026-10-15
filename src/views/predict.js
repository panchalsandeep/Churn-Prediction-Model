/* Predict single: a form built from the trained model's own columns, animated gauge, what-if simulator. */
import { $, state, escapeHtml, showToast, fmtMoney, on, replay, setBusy } from '../lib/ctx.js';
import { get, post } from '../lib/api.js';
import { tween, stagger } from '../lib/motion.js';
import { chartTheme } from '../lib/charts.js';
import { onThemeChange } from '../lib/theme.js';
import { createWhatIf } from '../lib/whatif.js';

let form = null;          // /api/model/schema
let lastRecs = '';
let lastRisk = '';
const gauge = { value: null, cancel: null };
let navigate = () => {};

/* ── Gauge ───────────────────────────────────────────────────── */
function drawGauge(probability) {
  const el = $('gaugeCanvas');
  const ctx = el.getContext('2d');
  const t = chartTheme();
  const W = 220, H = 130, cx = W / 2, cy = H - 18, r = 90;
  const dpr = window.devicePixelRatio || 1;
  if (el.width !== W * dpr) {
    el.width = W * dpr; el.height = H * dpr;
    el.style.width = `${W}px`; el.style.height = `${H}px`;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, 0, false);
  ctx.strokeStyle = t.track; ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();

  const pct = Math.max(0, Math.min(1, probability / 100));
  const end = Math.PI + pct * Math.PI;
  const grad = ctx.createLinearGradient(cx - r, cy, cx + r, cy);
  if (probability >= 50) { grad.addColorStop(0, '#E8A838'); grad.addColorStop(1, '#C23B3B'); }
  else if (probability >= 20) { grad.addColorStop(0, '#1F8A5B'); grad.addColorStop(1, '#E8A838'); }
  else { grad.addColorStop(0, '#2F6FED'); grad.addColorStop(1, '#1F8A5B'); }
  if (pct > 0) {
    ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, end, false);
    ctx.strokeStyle = grad; ctx.lineWidth = 16; ctx.lineCap = 'round'; ctx.stroke();
  }
  const mx = cx + r * Math.cos(end), my = cy + r * Math.sin(end);
  ctx.beginPath(); ctx.arc(mx, my, 10, 0, Math.PI * 2);
  ctx.fillStyle = t.dark ? '#142127' : '#FAF7F0'; ctx.fill();
  ctx.lineWidth = 3; ctx.strokeStyle = t.tooltipTitle; ctx.stroke();
  ctx.font = '500 10px "IBM Plex Sans"'; ctx.fillStyle = t.title; ctx.textAlign = 'center';
  ctx.fillText('0%', cx - r + 4, cy + 18);
  ctx.fillText('50%', cx, cy - r - 8);
  ctx.fillText('100%', cx + r - 4, cy + 18);
}

function animateGauge(probability) {
  gauge.cancel?.();
  const from = gauge.value ?? 0;
  gauge.value = probability;
  gauge.cancel = tween(from === 0 ? 1100 : 600, k => {
    const v = from + (probability - from) * k;
    drawGauge(v);
    $('gauge-val').textContent = `${v.toFixed(1)}%`;
  });
}
onThemeChange(() => { if (gauge.value != null) drawGauge(gauge.value); });

/* ── Rendering a prediction ─────────────────────────────────── */
function renderPrediction(data) {
  const content = $('result-content');
  if (content.style.display === 'none') {
    $('result-placeholder').style.display = 'none';
    content.style.display = 'block';
    replay(content, 'pop-in');
  }
  animateGauge(data.churn_probability);

  if (data.risk_level !== lastRisk) {
    lastRisk = data.risk_level;
    const badge = $('risk-badge-large');
    badge.className = `risk-badge-large ${data.risk_level}`;
    $('risk-label-large').textContent = `${data.risk_level} risk`;
    replay(badge, 'badge-pop');
  }
  $('result-economics').textContent = data.expected_loss != null
    ? `Expected loss ${fmtMoney(data.expected_loss)} · contacting this customer is worth ${fmtMoney(data.expected_net_value)} on average`
    : `Tier cut-offs: high ≥ ${(data.thresholds.high * 100).toFixed(0)}%, medium ≥ ${(data.thresholds.medium * 100).toFixed(0)}%`;

  // Probability-only updates (while dragging a slider) carry no explanation, so keep the current recommendations.
  const key = data.impacts ? data.recommendation.join('|') : lastRecs;
  if (key !== lastRecs) {
    lastRecs = key;
    const list = $('recs-list');
    list.innerHTML = data.recommendation.map(r => `<li>${escapeHtml(r)}</li>`).join('');
    stagger(list.children, 8);
  }
}

/* ── Form ───────────────────────────────────────────────────── */
// Column names come from the uploaded CSV, so they never go into markup raw: the element id is the
// name's UTF-8 bytes in hex (always [0-9a-f]), and every other attribute or text is escaped.
const fieldId = name => 'pf-' + Array.from(new TextEncoder().encode(String(name)), b => b.toString(16).padStart(2, '0')).join('');

function fieldHtml(f) {
  const id = fieldId(f.name);
  const label = `<label for="${id}">${escapeHtml(f.label)}</label>`;
  if (f.kind === 'category') {
    const opts = [...new Set([String(f.default), ...f.options])];
    return `<div class="form-group">${label}<select id="${id}" data-name="${escapeHtml(f.name)}">${
      opts.map(o => `<option ${o === String(f.default) ? 'selected' : ''}>${escapeHtml(o)}</option>`).join('')}</select></div>`;
  }
  const step = f.integer ? 1 : 'any';
  return `<div class="form-group">${label}<input type="number" id="${id}" data-name="${escapeHtml(f.name)}" value="${escapeHtml(f.default)}" step="${step}"
    title="Typical range ${escapeHtml(f.min)} – ${escapeHtml(f.max)}" /></div>`;
}

function buildForm(schema) {
  form = schema;
  const ranked = [...schema.fields].sort((a, b) => a.rank - b.rank);
  const main = ranked.slice(0, 10), rest = ranked.slice(10);
  $('predict-fields').innerHTML = `<div class="form-grid">${main.map(fieldHtml).join('')}</div>${
    rest.length ? `<details class="more-fields"><summary>${rest.length} more inputs</summary><div class="form-grid">${rest.map(fieldHtml).join('')}</div></details>` : ''}`;
  $('predict-empty').hidden = true;
  $('predict-btn').disabled = false;
}

function readForm() {
  const values = {};
  form.fields.forEach(f => {
    const el = $(fieldId(f.name));
    values[f.name] = f.kind === 'number' ? (el.value === '' ? null : Number(el.value)) : el.value;
  });
  return values;
}

const predictRequest = (values, { signal, explain = true } = {}) => post('/predict', { values, explain }, { signal });
const whatIf = createWhatIf({ score: predictRequest, render: renderPrediction });

/** Load a customer's known attributes into the form, then score them. */
export function simulateCustomer(id, attrs) {
  if (!form) { showToast('Train a model first.', 'error'); return; }
  const timeCol = state.results?.schema?.time_col;
  form.fields.forEach(f => {
    const el = $(fieldId(f.name));
    if (!el) return;
    let v = attrs[f.name];
    if (v == null && f.name.endsWith('_age_days')) {           // derived from a date column
      const base = attrs[f.name.slice(0, -9)];
      if (base) {
        const ref = timeCol && attrs[timeCol] ? new Date(attrs[timeCol]) : new Date();
        v = Math.round((ref - new Date(base)) / 86400000);
      }
    }
    if (v != null) el.value = v;
  });
  navigate('predict');
  $('predict-form').requestSubmit();
  showToast(`Loaded ${id}. Fields the file didn't contain use typical values.`);
}

export function initPredict({ navigate: nav }) {
  navigate = nav;
  $('predict-btn').disabled = true;
  $('predict-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (!form) { showToast('Please train a model first.', 'error'); return; }
    const btn = $('predict-btn');
    setBusy(btn, true, '<span class="spinner-inline"></span> Predicting…');
    const values = readForm();
    try {
      const data = await predictRequest(values);
      renderPrediction(data);
      whatIf.start(values, data, form.fields, form.correlated || []);
    } catch (err) {
      showToast(err.message || 'Prediction failed.', 'error');
    } finally {
      setBusy(btn, false);
    }
  });

  on('results', async () => {
    try { buildForm(await get('/model/schema')); } catch { /* shown when the user opens the page */ }
    $('result-content').style.display = 'none';
    $('result-placeholder').style.display = '';
    gauge.value = null; lastRecs = ''; lastRisk = '';
  });
}

