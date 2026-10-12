/* Admin console (/admin): username + password sign-in for platform administrators, then Firebase user management.
   Ported from feature/admin-login. The token is kept in sessionStorage (cleared when the tab closes). */

const KEY = 'churnsight-admin-token';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let users = [];

const getToken = () => { try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; } };
const setToken = t => { try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch { /* ignore */ } };

async function api(path, { method = 'GET', json } = {}) {
  const headers = {};
  const tok = getToken();
  if (tok) headers['X-Admin-Token'] = tok;
  if (json !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(`/api${path}`, { method, headers, body: json !== undefined ? JSON.stringify(json) : undefined });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && path !== '/admin/login') { setToken(''); show('login'); }
  if (!res.ok) throw Object.assign(new Error(data.error || `Request failed (${res.status})`), { status: res.status });
  return data;
}

function show(view) {
  $('login-view').hidden = view !== 'login';
  $('disabled-view').hidden = view !== 'disabled';
  $('console-view').hidden = view !== 'console';
}

function message(id, text, type = 'error') {
  const el = $(id);
  el.textContent = text;
  el.className = `console-msg ${type}`;
  el.hidden = !text;
}

function renderUsers() {
  const q = $('user-search').value.toLowerCase();
  const rows = users.filter(u => !q || [u.email, u.display_name, u.uid].some(v => v && v.toLowerCase().includes(q)));
  $('user-count').textContent = `(${users.length})`;
  $('user-list').innerHTML = rows.length ? rows.map(u => {
    const uid = esc(u.uid);
    const action = u.disabled ? 'enable' : 'disable';
    return `<tr><td>${esc(u.display_name || '—')}</td><td>${esc(u.email || '—')}</td>
      <td>${esc(u.provider_ids?.join(', ') || 'Email')}</td><td>${u.disabled ? 'Disabled' : 'Active'}</td>
      <td class="table-actions"><button class="btn-secondary btn-small" data-action="${action}" data-uid="${uid}" data-email="${esc(u.email || u.uid)}" type="button">${u.disabled ? 'Enable' : 'Disable'}</button>
      <button class="btn-secondary btn-small console-danger" data-action="delete" data-uid="${uid}" data-email="${esc(u.email || u.uid)}" type="button">Delete</button></td></tr>`;
  }).join('') : '<tr><td class="empty-row" colspan="5">No users found.</td></tr>';
}

async function loadUsers() {
  try {
    users = (await api('/admin/users')).users || [];
    renderUsers();
  } catch (err) {
    $('user-list').innerHTML = `<tr><td class="empty-row" colspan="5">${esc(err.message)}</td></tr>`;
  }
}

async function enterConsole(username) {
  $('console-who').textContent = `Signed in as ${username} (admin console).`;
  show('console');
  await loadUsers();
}

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  message('login-msg', '');
  const btn = $('login-btn');
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    const body = await api('/admin/login', { method: 'POST', json: { username: $('username').value.trim(), password: $('password').value } });
    setToken(body.token);
    $('password').value = '';
    await enterConsole(body.username);
  } catch (err) {
    message('login-msg', err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in as admin';
  }
});

$('forgot-btn').addEventListener('click', () => {
  const help = $('forgot-help');
  help.hidden = !help.hidden;
  $('forgot-btn').setAttribute('aria-expanded', String(!help.hidden));
});

$('logout-btn').addEventListener('click', () => {
  setToken('');
  users = [];
  show('login');
});

$('user-search').addEventListener('input', renderUsers);

$('user-table').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-uid]');
  if (!btn) return;
  const { action, uid, email } = btn.dataset;
  if (action === 'delete' && !window.confirm(`Delete the account ${email}? This cannot be undone.`)) return;
  btn.disabled = true;
  try {
    await api(`/admin/users/${encodeURIComponent(uid)}`, { method: 'PATCH', json: { action } });
    message('console-msg', `User ${email} ${action}d.`, 'success');
    await loadUsers();
  } catch (err) {
    message('console-msg', err.message);
    btn.disabled = false;
  }
});

(async function start() {
  let info = { enabled: false, is_admin: false };
  try { info = await api('/admin/session'); } catch { /* treat as signed out */ }
  if (info.is_admin) return enterConsole(info.username);
  setToken('');
  show(info.enabled ? 'login' : 'disabled');
})();
