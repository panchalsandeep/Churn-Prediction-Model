/* Upload view: schema preview wizard, training (async job with real progress), demo dataset. */
import { $, qsa, escapeHtml, showToast, replay, fmtInt } from '../lib/ctx.js';
import { request, runJob, ApiError } from '../lib/api.js';

let file = null;
let algorithm = 'auto';
let busy = false;
let previewOk = false;

const formatBytes = b => (b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`);

function showProgress(pct, label) {
  $('progress-wrap').hidden = false;
  $('progress-fill').style.width = `${pct}%`;
  $('progress-label').textContent = label;
  $('progress-pct').textContent = `${Math.round(pct)}%`;
}
const hideProgress = () => { $('progress-wrap').hidden = true; };

function setBusy(on) {
  busy = on;
  $('train-btn').disabled = on || !file || !previewOk;
  $('demo-btn').disabled = on;
  $('remove-file').disabled = on;
}

function renderSchema(res) {
  const s = res.schema;
  const box = $('schema-preview');
  const derived = s.dates.filter(d => d !== s.time_col).length;
  const row = (label, value, hint = '') =>
    `<div class="schema-item"><span>${label}</span><b>${value}</b>${hint ? `<small>${hint}</small>` : ''}</div>`;
  const warnings = s.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('');
  const dropped = s.dropped.slice(0, 6).map(d => `<span class="chip-static" title="${escapeHtml(d.reason)}">${escapeHtml(d.name)}</span>`).join('');
  box.innerHTML = `
    <div class="schema-title">Detected schema <span class="ok-dot"></span></div>
    <div class="schema-grid">
      ${row('Churn column', escapeHtml(s.target), `${(s.positive_rate * 100).toFixed(1)}% churned · ${res.estimated ? '≈' : ''}${fmtInt(s.rows)} rows`)}
      ${row('Customer ID', s.id ? escapeHtml(s.id) : 'none', s.id ? '' : 'rows will be numbered')}
      ${row('Holdout', s.time_col ? 'Time-based' : 'Stratified random', s.time_col ? `latest 20% of ${escapeHtml(s.time_col)}` : 'no snapshot date found')}
      ${row('Features', fmtInt(s.numeric.length + s.categorical.length + derived),
        `${s.numeric.length} numeric · ${s.categorical.length} categorical${derived ? ` · ${derived} from dates` : ''}`)}
      ${row('Revenue', s.revenue_col ? escapeHtml(s.revenue_col) : 'not found', s.revenue_col ? 'used for revenue at risk' : 'ranking uses risk only')}
    </div>
    ${dropped ? `<div class="schema-dropped"><span>Ignored:</span> ${dropped}${s.dropped.length > 6 ? ` +${s.dropped.length - 6}` : ''}</div>` : ''}
    ${warnings ? `<ul class="schema-warnings">${warnings}</ul>` : ''}`;
  box.hidden = false;
  replay(box, 'pop-in');
}

function renderSchemaError(message) {
  const box = $('schema-preview');
  box.innerHTML = `<div class="schema-title err">Cannot use this file</div><p class="schema-error">${escapeHtml(message)}</p>`;
  box.hidden = false;
  replay(box, 'pop-in');
}

async function setFile(f) {
  if (!f.name.toLowerCase().endsWith('.csv')) {
    showToast('Please upload a CSV file.', 'error');
    return;
  }
  file = f;
  previewOk = false;
  $('file-name-display').textContent = f.name;
  $('file-size-display').textContent = formatBytes(f.size);
  $('file-info').style.display = 'flex';
  replay($('file-info'), 'pop-in');
  $('drop-zone').style.display = 'none';
  $('schema-preview').hidden = true;
  $('train-btn').disabled = true;

  // Only the head of a big file is needed to understand its columns; training uploads the whole thing once.
  const PREVIEW_BYTES = 1.5 * 1024 * 1024;
  const form = new FormData();
  if (f.size > PREVIEW_BYTES) {
    form.append('file', f.slice(0, PREVIEW_BYTES), f.name);
    form.append('truncated', '1');
    form.append('total_bytes', String(f.size));
    form.append('sent_bytes', String(PREVIEW_BYTES));
  } else {
    form.append('file', f);
  }
  try {
    $('schema-preview').innerHTML = '<div class="schema-title">Reading your file…</div><div class="skeleton" style="width:70%"></div>';
    $('schema-preview').hidden = false;
    renderSchema(await request('/preview', { method: 'POST', form }));
    previewOk = true;
  } catch (err) {
    renderSchemaError(err.message);
  }
  setBusy(busy);
}

function clearFile() {
  file = null;
  previewOk = false;
  $('file-input').value = '';
  $('file-info').style.display = 'none';
  $('drop-zone').style.display = 'block';
  replay($('drop-zone'), 'pop-in');
  $('schema-preview').hidden = true;
  $('train-btn').disabled = true;
}

async function run(path, opts, label) {
  setBusy(true);
  showProgress(2, label);
  try {
    const result = await runJob(path, {
      ...opts,
      onProgress: job => showProgress(job.progress ?? 0, job.stage || label),
    });
    showProgress(100, 'Done');
    await new Promise(r => setTimeout(r, 350));
    return result;
  } finally {
    hideProgress();
    setBusy(false);
  }
}

export function initUpload({ onTrained }) {
  const drop = $('drop-zone');
  const input = $('file-input');
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragging'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('dragging');
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files[0]) setFile(input.files[0]); });
  $('remove-file').addEventListener('click', clearFile);

  qsa('.algo-pill').forEach(pill => pill.addEventListener('click', () => {
    qsa('.algo-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    algorithm = pill.dataset.algo;
  }));

  $('train-btn').addEventListener('click', async () => {
    if (!file || busy) return;
    const form = new FormData();
    form.append('file', file);
    form.append('algorithm', algorithm);
    try {
      const result = await run('/upload', { form }, 'Uploading…');
      await onTrained(result);
    } catch (err) {
      showToast(err.message, 'error');
    }
  });

  $('demo-btn').addEventListener('click', async () => {
    if (busy) return;
    try {
      const result = await run('/demo', { json: { rows: 8000, algorithm } }, 'Generating demo data…');
      await onTrained(result);
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : 'Demo failed.', 'error');
    }
  });
}

