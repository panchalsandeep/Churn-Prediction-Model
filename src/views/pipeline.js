/* Data pipeline (B2B accounts): tables, churn definition, schedule, alert channels, API keys, pull sources. All server-gated by role. */
import { $, state, escapeHtml, showToast, fmtInt, fmtPct, timeAgo, can, setBusy } from '../lib/ctx.js';
import { get, post, put, request, runJob } from '../lib/api.js';
import { confirmDialog, revealSecret } from '../lib/dialog.js';

const ROLE_HELP = {
  accounts: 'account_id, started_at (+ churned_at, contract_end, mrr, seats_licensed, any attributes)',
  events: 'account_id, ts (+ event, user_id, value)',
  tickets: 'account_id, ts (+ severity, status, csat)',
  invoices: 'account_id, ts, amount (+ status)',
};

const list = (items, render, empty) => (items.length ? items.map(render).join('') : `<p class="muted">${empty}</p>`);

async function loadStatus() {
  const s = await get('/pipeline');
  const tables = Object.entries(s.tables);
  $('pipe-tables').innerHTML = list(tables, ([name, t]) => `<div class="pipe-row"><b>${escapeHtml(name)}</b>
    <span>${fmtInt(t.rows)} rows · ${escapeHtml((t.min_ts || '').slice(0, 10))} to ${escapeHtml((t.max_ts || '').slice(0, 10))}</span></div>`, 'No tables yet. Upload accounts first, then events, tickets and invoices.');
  $('pipeline-sub').textContent = s.trained_at
    ? `Last trained ${timeAgo(s.trained_at)}${s.last_run ? ` · last ${s.last_run.kind} run ${timeAgo(s.last_run.at)} on ${fmtInt(s.last_run.rows)} rows` : ''}.`
    : 'Upload your tables, define churn, then train. Nothing leaves your workspace.';
  $('pipeline-run').disabled = !s.ready_to_train;
  if (s.target) { $('pipe-type').value = s.target.type; $('pipe-horizon').value = s.target.horizon_days; }
  const sch = s.schedule;
  $('pipe-sched-on').checked = !!sch?.enabled;
  if (sch) $('pipe-sched-hours').value = sch.every_hours;
  $('pipe-sched-status').textContent = sch ? `Next run ${new Date(sch.next_run_at * 1000).toLocaleString()}${sch.last_status ? ` · last: ${sch.last_status}` : ''}` : 'Not scheduled.';
}

function showPreview(p) {
  const rows = p.snapshots.map(r => `<tr><td>${escapeHtml(r.as_of)}</td><td>${fmtInt(r.accounts)}</td><td>${fmtInt(r.churned)}</td><td>${r.rate == null ? '–' : fmtPct(r.rate * 100)}</td></tr>`).join('');
  $('pipe-preview').innerHTML = `<p><b>${fmtInt(p.positives)}</b> churn events in <b>${fmtInt(p.labelled_rows)}</b> labelled account-months
    (base rate ${p.base_rate == null ? '–' : fmtPct(p.base_rate * 100)}). Data observed through ${escapeHtml(p.observed_through)}.</p>
    ${p.warnings.map(w => `<p class="pii-box">${escapeHtml(w)}</p>`).join('')}
    <div class="table-wrapper"><table class="customer-table"><thead><tr><th>As of</th><th>Accounts</th><th>Churned</th><th>Rate</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function loadChannels() {
  const { channels, email_configured: email } = await get('/channels');
  $('chan-kind').querySelector('option[value="email"]').disabled = !email;
  $('chan-list').innerHTML = list(channels, c => `<div class="pipe-row"><span><b>${escapeHtml(c.name)}</b> · ${escapeHtml(c.kind)} → ${escapeHtml(c.destination)}
    · ${escapeHtml(c.min_severity)}+ ${c.include_accounts ? '· with account details' : '· counts only'}
    ${c.last ? `<small class="${c.last.ok ? 'muted' : 'risk-tag High'}">${c.last.ok ? 'last send ok' : escapeHtml(c.last.detail)}</small>` : ''}</span>
    <span><button class="btn-secondary btn-small" data-chan-test="${c.id}" type="button">Test</button>
    <button class="btn-danger btn-small" data-chan-del="${c.id}" type="button">Remove</button></span></div>`, 'No alert channels yet.');
}

async function loadKeys() {
  const { keys } = await get('/api-keys');
  $('key-list').innerHTML = list(keys.filter(k => !k.revoked), k => `<div class="pipe-row"><span><b>${escapeHtml(k.name)}</b> · ${escapeHtml(k.scope)}
    <small class="muted">${k.last_used_at ? `used ${timeAgo(k.last_used_at)}` : 'never used'}</small></span>
    <button class="btn-danger btn-small" data-key-revoke="${k.id}" type="button">Revoke</button></div>`, 'No API keys.');
}

async function loadSources() {
  const { sources } = await get('/sources');
  $('src-list').innerHTML = list(sources, s => `<div class="pipe-row"><span><b>${escapeHtml(s.name)}</b> → ${escapeHtml(s.role)} · ${escapeHtml(s.host)}
    ${s.last ? `<small class="${s.last.ok ? 'muted' : 'risk-tag High'}">${escapeHtml(s.last.detail)}</small>` : ''}</span>
    <span><button class="btn-secondary btn-small" data-src-sync="${s.id}" type="button">Sync now</button>
    <button class="btn-danger btn-small" data-src-del="${s.id}" type="button">Remove</button></span></div>`, 'No pull sources.');
}

export async function loadPipeline() {
  if (!can('admin')) return;
  try { await Promise.all([loadStatus(), loadChannels(), loadKeys(), loadSources()]); } catch (err) { showToast(err.message, 'error'); }
}

const guard = (fn, ...after) => async e => {
  e?.preventDefault?.();
  try { await fn(e); await Promise.all(after.map(f => f())); } catch (err) { showToast(err.message, 'error'); }
};

export function initPipeline({ reloadResults }) {
  $('pipe-role').addEventListener('change', () => { $('pipe-role-help').textContent = `Columns: ${ROLE_HELP[$('pipe-role').value]}`; });
  $('pipe-role-help').textContent = `Columns: ${ROLE_HELP.accounts}`;

  $('pipe-file').addEventListener('change', guard(async () => {
    const file = $('pipe-file').files[0];
    const box = $('pipe-map');
    if (!file) { box.innerHTML = ''; return; }
    const form = new FormData();
    form.append('file', file);
    const p = await request(`/data/${$('pipe-role').value}/preview`, { method: 'POST', form });
    const fields = [...p.required, ...p.optional];
    const cell = f => {
      const m = p.mapping[f];
      const conf = p.confidence[f];
      const tone = m == null ? (p.required.includes(f) ? 'High' : '') : conf >= 0.85 ? 'Low' : 'Medium';
      return `<tr><td>${escapeHtml(f)}${p.required.includes(f) ? ' *' : ''}</td><td><select data-map="${f}"><option value="">(none)</option>${p.columns.map(c => `<option ${c === m ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}</select></td>
        <td><span class="risk-tag ${tone}">${m == null ? 'not found' : conf >= 0.85 ? 'matched' : 'please check'}</span></td></tr>`;
    };
    box.innerHTML = `<p class="muted">${fmtInt(p.rows_in_file)} rows, ${p.columns.length} columns. ${p.problem ? '' : `${fmtInt(p.rows_loadable)} would load.`}</p>
      ${p.problem ? `<p class="pii-box">${escapeHtml(p.problem)}</p>` : ''}
      ${p.personal_data_columns.length ? `<p class="pii-box">Personal-data columns will be left out: ${p.personal_data_columns.map(escapeHtml).join(', ')}</p>` : ''}
      <div class="table-wrapper"><table class="customer-table"><thead><tr><th>We need</th><th>Your column</th><th></th></tr></thead><tbody>${fields.map(cell).join('')}</tbody></table></div>`;
    const m = {};
    box.querySelectorAll('select[data-map]').forEach(s => { if (s.value) m[s.dataset.map] = s.value; });
    $('pipe-mapping').value = JSON.stringify(m);
  }));
  $('pipe-map').addEventListener('change', e => {
    if (!e.target.closest('select[data-map]')) return;
    const m = {};
    $('pipe-map').querySelectorAll('select[data-map]').forEach(s => { if (s.value) m[s.dataset.map] = s.value; });
    $('pipe-mapping').value = JSON.stringify(m);
  });

  $('pipe-upload-form').addEventListener('submit', guard(async () => {
    const file = $('pipe-file').files[0];
    if (!file) throw new Error('Choose a CSV file.');
    const form = new FormData();
    form.append('file', file);
    if ($('pipe-mapping').value.trim()) form.append('mapping', $('pipe-mapping').value.trim());
    form.append('mode', $('pipe-mode').value);
    const r = await request(`/data/${$('pipe-role').value}`, { method: 'POST', form });
    showToast(`${fmtInt(r.rows_added)} rows added to ${r.role}.`);
    $('pipe-file').value = '';
    $('pipe-map').innerHTML = '';
    $('pipe-mapping').value = '';
  }, loadStatus));

  const spec = () => ({ type: $('pipe-type').value, horizon_days: Number($('pipe-horizon').value) });
  $('pipe-preview-btn').addEventListener('click', guard(async () => showPreview(await post('/pipeline/target', spec()))));
  $('pipe-save-btn').addEventListener('click', guard(async () => { showPreview(await post('/pipeline/target', { ...spec(), save: true })); showToast('Churn definition saved.'); }, loadStatus));

  $('pipeline-run').addEventListener('click', async () => {
    const btn = $('pipeline-run');
    setBusy(btn, true, 'Training…');
    try { await runJob('/pipeline/run', { json: {} }); showToast('Model trained and accounts scored.'); await reloadResults?.(); await loadStatus(); }
    catch (err) { showToast(err.message, 'error'); } finally { setBusy(btn, false); }
  });

  $('pipe-sched-save').addEventListener('click', guard(async () => {
    const out = await put('/pipeline/schedule', { enabled: $('pipe-sched-on').checked, every_hours: Number($('pipe-sched-hours').value) });
    showToast(out.enabled ? 'Scheduled scoring is on.' : 'Scheduled scoring is off.');
  }, loadStatus));

  $('chan-form').addEventListener('submit', guard(async () => {
    const kind = $('chan-kind').value;
    const body = { kind, name: $('chan-name').value, min_severity: $('chan-sev').value, include_accounts: $('chan-accounts').checked };
    if (kind === 'email') body.to = $('chan-target').value.split(',').map(s => s.trim()).filter(Boolean); else body.url = $('chan-target').value.trim();
    const c = await post('/channels', body);
    if (c.signing_secret) await revealSecret('Signing secret, shown once. Use it to verify the X-ChurnSight-Signature header.', c.signing_secret, { title: 'Webhook signing secret' });
    $('chan-target').value = '';
    showToast('Channel added.');
  }, loadChannels));
  $('chan-list').addEventListener('click', guard(async e => {
    const t = e.target.closest('[data-chan-test]'); const d = e.target.closest('[data-chan-del]');
    if (t) { const r = await post(`/channels/${t.dataset.chanTest}/test`, {}); showToast(r.ok ? 'Test notification sent.' : `Failed: ${r.detail}`, r.ok ? 'success' : 'error'); }
    if (d && await confirmDialog('Remove this channel? Alerts will stop going there.', { title: 'Remove channel', confirmLabel: 'Remove', danger: true })) await request(`/channels/${d.dataset.chanDel}`, { method: 'DELETE' });
  }, loadChannels));

  $('key-form').addEventListener('submit', guard(async () => {
    const k = await post('/api-keys', { name: $('key-name').value, scope: 'ingest' });
    $('key-result').hidden = false;
    $('key-result').innerHTML = `<strong>Copy this key now. It is shown once.</strong><div class="invite-link"><code>${escapeHtml(k.key)}</code></div>
      <p class="muted">Send rows with <code>POST /api/ingest/events</code> and <code>Authorization: Bearer …</code>.</p>`;
    $('key-name').value = '';
  }, loadKeys));
  $('key-list').addEventListener('click', guard(async e => {
    const b = e.target.closest('[data-key-revoke]');
    if (b && await confirmDialog('Revoke this key? Systems using it stop working immediately.', { title: 'Revoke key', confirmLabel: 'Revoke', danger: true })) await request(`/api-keys/${b.dataset.keyRevoke}`, { method: 'DELETE' });
  }, loadKeys));

  $('src-form').addEventListener('submit', guard(async () => {
    const auth = $('src-auth').value.trim();
    await post('/sources', {
      name: $('src-name').value, role: $('src-role').value, url: $('src-url').value.trim(), format: $('src-format').value,
      headers: auth ? { Authorization: auth } : {}, records_path: $('src-records').value.trim(), next_path: $('src-next').value.trim(), since_param: $('src-since').value.trim(),
    });
    $('src-url').value = ''; $('src-auth').value = '';
    showToast('Source added. Use Sync now to pull it.');
  }, loadSources));
  $('src-list').addEventListener('click', guard(async e => {
    const s = e.target.closest('[data-src-sync]'); const d = e.target.closest('[data-src-del]');
    if (s) { const r = await post(`/sources/${s.dataset.srcSync}/sync`, {}); showToast(r.detail, r.ok ? 'success' : 'error'); }
    if (d && await confirmDialog('Remove this source? Data already loaded stays.', { title: 'Remove source', confirmLabel: 'Remove', danger: true })) await request(`/sources/${d.dataset.srcDel}`, { method: 'DELETE' });
  }, loadSources, loadStatus));
}
