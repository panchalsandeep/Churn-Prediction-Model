/* Team & access, privacy tools, audit chain. Everything here is gated by role on the server; the UI just hides what you cannot use. */
import { $, state, escapeHtml, showToast, fmtInt, timeAgo, can, replay } from '../lib/ctx.js';
import { get, post, patch, request, download } from '../lib/api.js';

const ROLES = ['viewer', 'analyst', 'admin', 'owner'];
const ROLE_HELP = {
  viewer: 'Read dashboards and customers',
  analyst: '+ export, work alerts, log actions',
  admin: '+ train, score, settings, manage people',
  owner: '+ manage admins, SSO, delete workspace',
};

/** Roles the current user is allowed to hand out (owners: all; admins: analyst and viewer). */
const grantable = () => (state.me.role === 'owner' ? ROLES : ['viewer', 'analyst']);

async function loadMembers() {
  const { members, seats } = await get('/members');
  $('team-sub').textContent = `${seats} active seat${seats === 1 ? '' : 's'}. Invitations are single-use, expire in 7 days, and only work for the invited email.`;
  const mine = state.me.uid;
  $('members-body').innerHTML = members.map(m => {
    const isMe = m.uid === mine;
    const editable = !isMe && (state.me.role === 'owner' || ['viewer', 'analyst'].includes(m.role));
    const select = editable
      ? `<select class="role-select" data-uid="${escapeHtml(m.uid)}" aria-label="Role for ${escapeHtml(m.email || m.uid)}">${
        (state.me.role === 'owner' ? ROLES : ['viewer', 'analyst']).map(r => `<option ${r === m.role ? 'selected' : ''}>${r}</option>`).join('')}</select>`
      : `<span class="risk-tag ${m.role === 'owner' ? 'Low' : ''}">${escapeHtml(m.role)}</span>`;
    return `<tr><td><strong>${escapeHtml(m.email || m.uid)}</strong>${isMe ? ' <small class="muted">(you)</small>' : ''}</td><td>${select}</td>
      <td>${timeAgo(m.since)}</td>
      <td class="table-actions">${editable || isMe ? `<button class="btn-danger btn-small" data-remove="${escapeHtml(m.uid)}" type="button">${isMe ? 'Leave' : 'Remove'}</button>` : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="4" class="empty-row">No members.</td></tr>';
  $('invite-role').innerHTML = grantable().map(r => `<option value="${r}">${r} · ${ROLE_HELP[r]}</option>`).join('');
}

const METRIC_LABEL = { scored_accounts: 'Accounts scored', training_runs: 'Training runs', api_calls: 'API calls' };

function meter(label, used, limit, hard) {
  const pct = limit == null ? 0 : Math.min(100, (used / Math.max(limit, 1)) * 100);
  const tone = limit != null && used >= limit ? 'High' : pct >= 80 ? 'Medium' : 'Low';
  return `<div class="meter"><div class="meter-head"><span>${escapeHtml(label)}</span>
    <b>${fmtInt(used)}${limit == null ? ' · unlimited' : ` / ${fmtInt(limit)}`}</b></div>
    <div class="meter-bar" role="progressbar" aria-label="${escapeHtml(label)}" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"><i class="${tone}" style="width:${pct}%"></i></div>
    ${limit != null && !hard && used > limit ? '<small class="muted">Over the included amount: still served, will be billed as overage.</small>' : ''}</div>`;
}

async function loadUsage() {
  const u = await get('/usage');
  $('usage-sub').textContent = `${u.plan[0].toUpperCase()}${u.plan.slice(1)} plan · resets monthly (${u.period}, UTC). Limits marked hard stop new work; API calls never block.`;
  const s = u.seats;
  $('usage-meters').innerHTML = meter('Seats (members + pending invitations)', s.used + s.pending, s.limit, true)
    + Object.entries(u.metrics).map(([k, m]) => meter(METRIC_LABEL[k] || k, m.used, m.limit, m.hard)).join('')
    + (u.rows_per_upload ? `<p class="muted">Largest dataset per upload: ${fmtInt(u.rows_per_upload)} rows.</p>` : '');
}

async function loadPendingInvites() {
  const { invites } = await get('/invites');
  const box = $('pending-invites');
  box.hidden = !invites.length;
  box.innerHTML = invites.length ? `<h4>Pending invitations</h4><ul>${invites.map(i => `
    <li><span>${escapeHtml(i.email)} · ${escapeHtml(i.role)}</span><small>expires ${new Date(i.expires_at * 1000).toLocaleDateString()}</small>
    <button class="btn-danger btn-small" data-revoke="${escapeHtml(i.id)}" type="button">Revoke</button></li>`).join('')}</ul>` : '';
}

async function loadAudit() {
  const { events, chain } = await get('/audit');
  const badge = $('chain-badge');
  badge.textContent = chain.ok ? `Chain verified · ${fmtInt(chain.checked)} entries` : `TAMPERING DETECTED at entry ${chain.first_bad_id}`;
  badge.className = `risk-tag ${chain.ok ? 'Low' : 'High'}`;
  $('audit-list').innerHTML = events.length ? events.slice(0, 30).map(e => `
    <li><b>${escapeHtml(e.action)}</b><span>${escapeHtml(e.actor || '')}</span><small>${timeAgo(e.ts)}</small></li>`).join('')
    : '<li class="empty-card">No activity yet.</li>';
}

export async function loadTeam() {
  const tasks = [];
  if (can('admin')) tasks.push(loadMembers(), loadAudit(), loadUsage(), loadPendingInvites());
  $('sso-required').checked = !!state.me.workspace?.sso_required;
  try { await Promise.all(tasks); } catch (err) { showToast(err.message, 'error'); }
}

function showInvite(inv) {
  const box = $('invite-result');
  const link = `${location.origin}/?invite=${encodeURIComponent(inv.token)}`;
  box.hidden = false;
  box.innerHTML = `<strong>Invitation for ${escapeHtml(inv.email)} (${escapeHtml(inv.role)})</strong>
    <p>Share this link once, over a private channel. It cannot be shown again.</p>
    <div class="invite-link"><code id="invite-link-text">${escapeHtml(link)}</code><button class="btn-secondary btn-small" id="invite-copy" type="button">Copy</button></div>`;
  replay(box, 'pop-in');
  $('invite-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(link); showToast('Invitation link copied.'); } catch { showToast('Select and copy the link manually.', 'error'); }
  });
}

export function initTeam({ reloadResults }) {
  $('invite-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      showInvite(await post('/invites', { email: $('invite-email').value, role: $('invite-role').value }));
      $('invite-email').value = '';
      loadAudit();
      loadUsage().catch(() => {});
      loadPendingInvites().catch(() => {});
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('pending-invites').addEventListener('click', async e => {
    const b = e.target.closest('[data-revoke]');
    if (!b) return;
    try { await request(`/invites/${encodeURIComponent(b.dataset.revoke)}`, { method: 'DELETE' }); showToast('Invitation revoked.'); loadPendingInvites(); loadUsage(); loadAudit(); }
    catch (err) { showToast(err.message, 'error'); }
  });

  $('usage-export').addEventListener('click', () => download('/usage/export', 'usage.csv').catch(err => showToast(err.message, 'error')));

  $('members-body').addEventListener('change', async e => {
    const sel = e.target.closest('.role-select');
    if (!sel) return;
    try { await patch(`/members/${encodeURIComponent(sel.dataset.uid)}`, { role: sel.value }); showToast('Role updated.'); loadAudit(); }
    catch (err) { showToast(err.message, 'error'); loadMembers(); }
  });

  $('members-body').addEventListener('click', async e => {
    const btn = e.target.closest('[data-remove]');
    if (!btn) return;
    const leaving = btn.dataset.remove === state.me.uid;
    if (!window.confirm(leaving ? 'Leave this workspace?' : 'Remove this member? They lose access immediately.')) return;
    try {
      await request(`/members/${encodeURIComponent(btn.dataset.remove)}`, { method: 'DELETE' });
      showToast(leaving ? 'You left the workspace.' : 'Member removed.');
      if (leaving) location.reload(); else { loadMembers(); loadAudit(); }
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('sso-required').addEventListener('change', async e => {
    try {
      await post('/workspace/sso', { required: e.target.checked });
      showToast(e.target.checked ? 'Single sign-on is now required.' : 'Single sign-on is no longer required.');
    } catch (err) { showToast(err.message, 'error'); e.target.checked = !e.target.checked; }
  });

  $('audit-export').addEventListener('click', () => download('/audit/export', 'audit-log.ndjson').catch(err => showToast(err.message, 'error')));

  /* Privacy tools */
  $('hash-ids').addEventListener('change', async e => {
    try { await post('/settings', { privacy: { hash_ids: e.target.checked } }); showToast(e.target.checked ? 'New uploads will use pseudonymised customer IDs.' : 'New uploads will keep your customer IDs.'); }
    catch (err) { showToast(err.message, 'error'); e.target.checked = !e.target.checked; }
  });

  $('erase-form').addEventListener('submit', async e => {
    e.preventDefault();
    const ids = $('erase-ids').value.split(/[\s,;]+/).filter(Boolean);
    if (!ids.length) return;
    if (!window.confirm(`Permanently erase ${ids.length} customer(s) from live data and every stored model snapshot?`)) return;
    try {
      const r = await post('/privacy/erase', { ids });
      $('erase-ids').value = '';
      showToast(`Erased ${fmtInt(r.erased)} record(s); ${r.snapshots_rewritten} snapshot(s) rewritten.`);
      await reloadResults();
      loadAudit();
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('dsar-form').addEventListener('submit', async e => {
    e.preventDefault();
    const id = $('dsar-id').value.trim();
    if (!id) return;
    try {
      const data = await get(`/privacy/export/${encodeURIComponent(id)}`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const a = document.createElement('a');
      a.href = url; a.download = `customer-${id}.json`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('delete-workspace').addEventListener('click', async () => {
    const id = state.me.workspace.id;
    const typed = window.prompt(`This permanently deletes ALL data in this workspace and destroys its encryption key.\nType the workspace id to confirm:\n${id}`);
    if (typed !== id) { if (typed !== null) showToast('Workspace id did not match; nothing was deleted.', 'error'); return; }
    try { await request('/workspace', { method: 'DELETE', json: { confirm: id } }); location.reload(); }
    catch (err) { showToast(err.message, 'error'); }
  });

}
