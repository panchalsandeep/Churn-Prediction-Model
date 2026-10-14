/* ═══════════════════════════════════════════════════════════════
   ChurnSight – frontend bootstrap.
   Navigation, auth and results loading live here; each page is a module in src/views/.
═══════════════════════════════════════════════════════════════ */

import { loadFirebase } from './lib/firebase.js';
import { initTheme, toggleTheme } from './lib/theme.js';
import { initPalette } from './lib/palette.js';
import { initFeedback } from './views/feedback.js';
import { debounce, replay, prefersReducedMotion } from './lib/motion.js';
import { $, qs, qsa, state, escapeHtml, showToast, emit, fmtPct, can } from './lib/ctx.js';
import { get, post, ApiError } from './lib/api.js';
import { renderSectionCharts, markAllDirty, destroyAll, rebuildOnThemeChange } from './lib/chartkit.js';

import { initUpload } from './views/upload.js';
import { initOverview } from './views/overview.js';
import { initAnalytics } from './views/analytics.js';
import { initCohorts } from './views/cohorts.js';
import { initCustomers, loadTable, setTierFilter, focusSearch, exportAll } from './views/customers.js';
import { initDrawer, openCustomer } from './views/drawer.js';
import { initHealth } from './views/health.js';
import { initAlerts, refreshAlerts } from './views/alerts.js';
import { initPredict, simulateCustomer } from './views/predict.js';
import { initAdmin, loadAdminPanel } from './views/admin.js';

let fb = null;   // Firebase, loaded on demand

/* ══════════════════  NAVIGATION  ═════════════════════════════ */
const sections = ['upload', 'overview', 'health', 'cohorts', 'alerts', 'analytics', 'customers', 'predict', 'admin'];
const titles = {
  upload: 'Upload & Train Model',
  overview: 'Overview Dashboard',
  health: 'Account Health',
  cohorts: 'Cohort Analysis',
  alerts: 'Alerts & Actions',
  analytics: 'Analytics & Model Performance',
  customers: 'Customer Risk List',
  predict: 'Predict Single Customer',
  admin: 'Workspace Settings',
};
const NEEDS_MODEL = new Set(['overview', 'health', 'cohorts', 'alerts', 'analytics', 'customers']);

let navLock = false;

/* Elements that fade/slide in (staggered) whenever their section opens. */
const REVEAL_SELECTOR = [
  '.reveal', '.card', '.kpi-card', '.insight-tile', '.upload-hero', '.health-header',
  '.cohort-toolbar', '.alerts-toolbar', '.customers-toolbar',
].join(',');

function revealSection(section) {
  const nodes = qsa(`#section-${section} ${REVEAL_SELECTOR.split(',').join(`, #section-${section} `)}`)
    .filter(node => !node.parentElement.closest(REVEAL_SELECTOR) || node.classList.contains('reveal'));
  nodes.forEach((node, i) => {
    node.style.setProperty('--delay', String(Math.min(i, 9)));
    replay(node, 'reveal');
  });
}

function moveNavIndicator() {
  const indicator = $('nav-indicator');
  const active = qs('.nav-item.active');
  if (!indicator || !active) return;
  indicator.style.transform = `translateY(${active.offsetTop}px)`;
  indicator.style.height = `${active.offsetHeight}px`;
  indicator.classList.add('is-ready');
}

function setPageTitle(text) {
  const el = $('page-title');
  el.textContent = text;
  replay(el, 'title-swap');
}

function navigateTo(section) {
  if (navLock) return;

  const apply = () => {
    sections.forEach(s => {
      const navItem = $(`nav-${s}`);
      const el = $(`section-${s}`);
      el.classList.remove('is-leaving');
      el.classList.toggle('active', s === section);
      navItem.classList.toggle('active', s === section);
      if (s === section) navItem.setAttribute('aria-current', 'page');
      else navItem.removeAttribute('aria-current');
    });
    state.currentSection = section;
    setPageTitle(titles[section]);
    moveNavIndicator();
    revealSection(section);
    renderSectionCharts(section);
    if (section === 'customers') loadTable();
    if (section === 'alerts') refreshAlerts();
    if (section === 'admin') loadAdminPanel();
    if (window.innerWidth <= 768) qs('.sidebar').classList.remove('open');
    window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    navLock = false;
  };

  const leaving = $(`section-${state.currentSection}`);
  if (section === state.currentSection || prefersReducedMotion() || !leaving?.classList.contains('active')) {
    apply();
    return;
  }
  navLock = true;
  leaving.classList.add('is-leaving');
  leaving.classList.remove('active');
  window.setTimeout(apply, 180);
}

/** Navigation with the same guards as clicking the sidebar. */
function requestNavigate(sec) {
  if (!state.results && NEEDS_MODEL.has(sec)) {
    showToast('Upload data and train a model first, or try the demo dataset.', 'error');
    return;
  }
  navigateTo(sec);
}

qsa('.nav-item').forEach(el => el.addEventListener('click', e => {
  e.preventDefault();
  requestNavigate(el.dataset.section);
}));
window.addEventListener('resize', debounce(moveNavIndicator, 120));
$('menu-toggle').addEventListener('click', () => qs('.sidebar').classList.toggle('open'));

/* ══════════════════  CLOCK  ═══════════════════════════════════ */
function updateClock() {
  if (document.hidden) return;
  $('time-display').textContent = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
updateClock();
setInterval(updateClock, 1000);

/* ══════════════════  RESULTS  ═════════════════════════════════ */
function updateModelStatus(model) {
  $('model-status-badge').innerHTML = model
    ? `<span class="status-dot active"></span><span>${escapeHtml(model.algorithm)} live</span>`
    : '<span class="status-dot inactive"></span><span>No model loaded</span>';
  const badge = $('algo-badge');
  badge.style.display = model ? 'flex' : 'none';
  if (model) { badge.textContent = model.algorithm; replay(badge, 'pop-in'); }
}

/* ══════════════════  IDENTITY, ROLES, ACCESS GATE  ═════════════════ */
const GATES = {
  mfa_required: ['Multi-factor authentication required', 'Your role needs a second sign-in factor. Enrol one in your account security settings, sign in again, and complete the prompt.'],
  sso_required: ['Single sign-on required', 'This workspace only allows sign-in through your company identity provider. Use your organisation\'s SSO sign-in link.'],
  no_workspace: ['No workspace yet', 'This account is not part of a workspace. Paste the invitation code your administrator sent you, or ask them to invite you.'],
  forbidden_workspace: ['Workspace not available', 'You do not have access to that workspace.'],
};

function showGate(code, fallback) {
  const [title, text] = GATES[code] || ['Access needed', fallback || 'Access is not available for this account.'];
  $('gate-title').textContent = title;
  $('gate-text').textContent = text;
  $('gate-invite').hidden = code !== 'no_workspace';
  $('access-gate').hidden = false;
  $('access-gate').style.display = 'flex';
  document.body.classList.add('app-locked');
}

function applyRole() {
  document.body.dataset.role = state.me.role;
  qsa('[data-min-role]').forEach(el => { el.hidden = !can(el.dataset.minRole); });
  $('upload-denied').hidden = can('admin');
  $('workspace-badge').textContent = state.me.role;
}

/** Who am I and what may I do? Returns false (and shows the gate) when access is not allowed yet. */
async function loadMe() {
  try {
    state.me = await get('/me');
    state.workspaceId = state.me.workspace.id;
    applyRole();
    emit('me', state.me);
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.status === 403 && err.code) { showGate(err.code, err.message); return false; }
    console.warn('Could not load identity:', err);
    return true;       // do not lock people out because of a network blip
  }
}

async function acceptInviteFromUrl() {
  const token = new URLSearchParams(location.search).get('invite');
  if (!token) return;
  history.replaceState({}, '', location.pathname);
  try { await post('/invites/accept', { token }); showToast('Invitation accepted. Welcome to the workspace!'); }
  catch (err) { showToast(err.message, 'error'); }
}

function applyResults(results) {
  state.results = results;
  updateModelStatus(results?.model);
  destroyAll();
  markAllDirty();
  if (results) {
    emit('results', results);
    renderSectionCharts(state.currentSection);
  }
}

/** Fetch the champion model's results. Returns false when the workspace has no model yet. */
async function reloadResults() {
  try {
    const results = await get('/results');
    if (results) applyResults(results);
    return true;
  } catch (err) {
    if (err.status === 404) { applyResults(null); return false; }
    if (err.code === 'model_unavailable') {      // e.g. a model saved in the old, unsafe pickle format
      applyResults(null);
      showToast(err.message, 'error');
      return false;
    }
    console.warn('Unable to load results:', err);
    return false;
  }
}

async function onTrained(result) {
  await reloadResults();
  const m = result?.metrics;
  if (result?.status === 'challenger') {
    showToast(`Trained, but kept as challenger: ${result.reason}.`, 'error');
  } else if (m) {
    showToast(`Model ready · ROC-AUC ${fmtPct(m.roc_auc, 1)} · PR-AUC ${fmtPct(m.pr_auc, 1)} (${(m.pr_auc / Math.max(m.pr_auc_baseline, 0.01)).toFixed(1)}× baseline)`);
  }
  navigateTo('overview');
}

/* ══════════════════  VIEWS  ═══════════════════════════════════ */
initUpload({ onTrained });
initOverview();
initAnalytics();
initCohorts();
initCustomers();
initDrawer({ onSimulate: simulateCustomer });
initHealth();
initAlerts();
initPredict({ navigate: requestNavigate });
initAdmin({ reloadResults, navigate: navigateTo });
rebuildOnThemeChange(() => state.currentSection);

$('retrain-btn').addEventListener('click', () => { navigateTo('upload'); showToast('Upload new data to retrain. The current model stays live until you do.'); });
$('export-overview-btn').addEventListener('click', exportAll);
qsa('.kpi-card[data-risk]').forEach(card => card.addEventListener('click', () => {
  navigateTo('customers');
  setTierFilter(card.dataset.risk);
}));

/* ══════════════════  FIREBASE AUTH  ════════════════════════ */
let activeAuthTab = 'login';

const SESSION_HINT = 'churnsight-session';

/** Signed out (or session expired): show the login card. The overlay is already in the first paint. */
function showAuthOverlay() {
  const overlay = $('auth-overlay');
  document.documentElement.classList.remove('returning');
  document.body.classList.add('app-locked');
  overlay.style.display = 'flex';
  overlay.style.opacity = '1';
  overlay.setAttribute('aria-hidden', 'false');
  overlay.classList.add('is-visible');
}

/** Signed in: reveal the app and fade the overlay away. */
function hideAuthOverlay() {
  const overlay = $('auth-overlay');
  document.body.classList.remove('app-locked');
  overlay.style.opacity = '0';
  overlay.setAttribute('aria-hidden', 'true');
  setTimeout(() => { overlay.style.display = 'none'; }, 380);
}

function setAuthTab(tab) {
  activeAuthTab = tab;
  $('tab-login').classList.toggle('active', tab === 'login');
  $('tab-register').classList.toggle('active', tab === 'register');
  const login = tab === 'login';
  $('auth-subtitle').textContent = login ? 'Sign in to access your churn prediction workspace' : 'Create an account to train models and run retention playbooks';
  $('submit-btn-text').textContent = login ? 'Sign In' : 'Create Account';
  qs('.auth-title').textContent = login ? 'Welcome back' : 'Join ChurnSight';
  $('forgot-password-row').hidden = !login;
}

/** Send a Firebase password-reset email. Same message whether or not the account exists (no account enumeration). */
async function handleForgotPassword() {
  if (!fb) { showToast('Sign-in is still loading. Try again in a moment.', 'error'); return; }
  const email = $('auth-email').value.trim();
  if (!email) {
    showToast('Enter your email address first, then click "Forgot your password?".', 'error');
    $('auth-email').focus();
    return;
  }
  const btn = $('forgot-password-btn');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    // Branded email from our server; fall back to Firebase's own email when the server has no mail settings.
    try {
      await post('/auth/password-reset', { email });
    } catch (err) {
      if (err?.status === 501 || err?.status === 0) await fb.sendPasswordResetEmail(fb.auth, email);
      else throw err;
    }
    showToast(`If an account exists for ${email}, a reset link is on its way. Check your inbox.`);
  } catch (err) {
    if (err?.code === 'auth/user-not-found') showToast(`If an account exists for ${email}, a reset link is on its way. Check your inbox.`);
    else showToast(err?.code === 'auth/invalid-email' ? 'That email address is not valid.' : err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Forgot your password?';
  }
}

async function handleEmailAuth(e) {
  e.preventDefault();
  if (!fb) { showToast('Sign-in is still loading. Try again in a moment.', 'error'); return; }
  const email = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const btn = $('auth-submit-btn');
  btn.disabled = true;
  const original = $('submit-btn-text').textContent;
  $('submit-btn-text').innerHTML = `<span class="spinner-inline"></span> ${activeAuthTab === 'login' ? 'Signing In...' : 'Registering...'}`;
  try {
    if (activeAuthTab === 'login') await fb.signInWithEmailAndPassword(fb.auth, email, password);
    else {
      const cred = await fb.createUserWithEmailAndPassword(fb.auth, email, password);
      sendVerificationEmail(cred.user);
    }
  } catch (err) {
    showToast(err.message, 'error');
    btn.disabled = false;
    $('submit-btn-text').textContent = original;
  }
}

/** Ask the server for a branded verification email; fall back to Firebase's own email if mail is not configured. */
async function sendVerificationEmail(user) {
  if (!user || user.emailVerified) return;
  try {
    await post('/auth/send-verification', {});
  } catch (err) {
    if (err?.status === 501) { try { await fb.sendEmailVerification(user); } catch { /* best effort */ } }
  }
  showToast(`We sent a confirmation link to ${user.email}. Please check your inbox.`);
}

async function handleGoogleAuth() {
  if (!fb) { showToast('Sign-in is still loading. Try again in a moment.', 'error'); return; }
  try {
    await fb.signInWithPopup(fb.auth, new fb.GoogleAuthProvider());
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function handleSignOut() {
  try {
    await fb.signOut(fb.auth);
    showToast('Signed out successfully.');
    location.reload();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function refreshAdminAccess() {
  try {
    const body = await get('/admin/whoami');
    state.admin.isAdmin = body.is_admin === true;
  } catch {
    state.admin.isAdmin = false;
  }
}

async function initAuthUI() {
  $('tab-login').addEventListener('click', () => setAuthTab('login'));
  $('tab-register').addEventListener('click', () => setAuthTab('register'));
  $('auth-form').addEventListener('submit', handleEmailAuth);
  $('google-login-btn').addEventListener('click', handleGoogleAuth);
  $('forgot-password-btn').addEventListener('click', handleForgotPassword);
  $('logout-btn').addEventListener('click', handleSignOut);

  // The auth card is already interactive; the SDK streams in behind it.
  try {
    fb = await loadFirebase();
  } catch {
    showAuthOverlay();   // drop the splash so the card (and the error) is visible
    showToast('Could not load sign-in. Check your connection and reload.', 'error');
    return;
  }

  fb.onAuthStateChanged(fb.auth, async user => {
    if (user) {
      state.user = user;
      const initials = user.displayName
        ? user.displayName.split(' ').map(n => n[0]).join('').slice(0, 2)
        : (user.email ? user.email.slice(0, 2) : '?');
      $('user-avatar').textContent = initials.toUpperCase();
      $('user-display-name').textContent = user.displayName || 'Team Member';
      $('user-display-email').textContent = user.email;
      $('user-profile-section').style.display = 'flex';
      try { localStorage.setItem(SESSION_HINT, '1'); } catch { /* ignore */ }
      hideAuthOverlay();
      showToast(`Welcome back, ${user.displayName || user.email}!`);
      await refreshAdminAccess();
      await acceptInviteFromUrl();
      if (await loadMe()) { $('access-gate').hidden = true; await reloadResults(); }
    } else {
      state.user = null;
      state.admin.isAdmin = false;
      $('user-profile-section').style.display = 'none';
      try { localStorage.removeItem(SESSION_HINT); } catch { /* ignore */ }
      showAuthOverlay();
      if (await loadMe()) await reloadResults();
    }
  });
}

$('gate-signout').addEventListener('click', () => (fb ? handleSignOut() : location.reload()));
$('gate-invite').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await post('/invites/accept', { token: $('gate-token').value.trim() });
    location.reload();
  } catch (err) { showToast(err.message, 'error'); }
});

/* ══════════════════  COMMAND PALETTE  ═════════════════════ */
initTheme();
initFeedback();
initPalette({
  navigate: requestNavigate,
  focusSearch() {
    if (!state.results) { showToast('Train a model to search customers.', 'error'); return; }
    navigateTo('customers');
    setTimeout(focusSearch, prefersReducedMotion() ? 0 : 220);
  },
  toggleTheme: () => toggleTheme(),
  async findCustomers(query) {
    if (!state.results) return [];
    const res = await get(`/customers?q=${encodeURIComponent(query)}&limit=6&sort=loss`);
    return res.customers;
  },
  openCustomer(id) {
    navigateTo('customers');
    openCustomer(id);
  },
  getCommands() {
    const nav = Object.entries(titles).map(([key, label]) => ({
      id: `go:${key}`, label: `Go to ${label}`, group: 'Pages', keywords: key, run: () => requestNavigate(key),
    }));
    return [
      ...nav,
      { id: 'theme', label: 'Toggle dark theme', group: 'Actions', hint: 'D', keywords: 'light dark mode appearance', run: () => toggleTheme() },
      { id: 'export', label: 'Export risk list as CSV', group: 'Actions', keywords: 'download', run: exportAll },
      { id: 'retrain', label: 'Train a new model', group: 'Actions', keywords: 'upload retrain', run: () => navigateTo('upload') },
      { id: 'high', label: 'Show high-risk customers', group: 'Actions', keywords: 'filter churn', run: () => {
        if (!state.results) { showToast('Upload data and train a model first.', 'error'); return; }
        navigateTo('customers');
        setTierFilter('High');
      } },
      ...(state.user ? [{ id: 'signout', label: 'Sign out', group: 'Actions', run: handleSignOut }] : []),
    ];
  },
});

// Fonts and late layout shifts move the nav items; keep the indicator aligned.
document.fonts?.ready.then(moveNavIndicator);
window.addEventListener('load', moveNavIndicator);

initAuthUI();
navigateTo('upload');
