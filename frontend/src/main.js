import QrScanner from 'qr-scanner';
import './style.css';

const app = document.querySelector('#app');
let csrfToken = '';
let sessionUser = null;
let activePage = 'dashboard';
let scannerInstance = null;
let scannerBusy = false;
let selectedFile = null;
let importInspection = null;
let importPreview = null;
let templateInfo = null;
let placements = null;
let selectedLayer = 'qr';
let layerDrag = null;

let isScannerOnlyMode = false;
let soundEnabled = localStorage.getItem('confluence_sound') !== 'false';
let currentCamera = 'environment';

const NAV = [
  { id: 'dashboard', icon: '⌂', label: 'Overview', group: 'EVENT' },
  { id: 'import', icon: '⇧', label: 'Import students', group: 'EVENT' },
  { id: 'template', icon: '▧', label: 'Ticket design', group: 'TICKETS' },
  { id: 'generate', icon: '▤', label: 'Generate tickets', group: 'TICKETS' },
  { id: 'scanner', icon: '⌗', label: 'Scan attendance', group: 'ENTRY' },
  { id: 'attendees', icon: '☷', label: 'Attendee list', group: 'ENTRY' },
  { id: 'report', icon: '⇩', label: 'Attendance report', group: 'REPORTS' },
  { id: 'account', icon: '⚙', label: 'My account', group: 'SETTINGS' },
  { id: 'staff', icon: '♙', label: 'Staff & access', group: 'SETTINGS', admin: true },
];

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
const initials = (value) => String(value || 'U').split(/[@.\s_-]+/).filter(Boolean).slice(0,2).map(x=>x[0].toUpperCase()).join('') || 'U';
const fmtDate = (value) => value ? new Intl.DateTimeFormat('en-IN', { dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Kolkata' }).format(new Date(value)) : '—';
const fmtNumber = (n) => new Intl.NumberFormat('en-IN').format(Number(n || 0));

function playChime(type = 'success') {
  if (!soundEnabled) return;
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    if (type === 'success') {
      osc.type = 'sine';
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(1320, ctx.currentTime + 0.15);
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
      osc.start();
      osc.stop(ctx.currentTime + 0.25);
    } else {
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(220, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(110, ctx.currentTime + 0.2);
      gain.gain.setValueAtTime(0.4, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.35);
      osc.start();
      osc.stop(ctx.currentTime + 0.35);
    }
  } catch (_) {}
}

function triggerHaptic(type = 'success') {
  if (navigator.vibrate) {
    try {
      navigator.vibrate(type === 'success' ? [120] : [200, 100, 200]);
    } catch (_) {}
  }
}

function toast(message, type = '') {
  let stack = document.querySelector('.toast-stack');
  if (!stack) { stack = document.createElement('div'); stack.className = 'toast-stack'; document.body.appendChild(stack); }
  const item = document.createElement('div'); item.className = `toast ${type}`; item.textContent = message; stack.appendChild(item);
  setTimeout(() => { item.remove(); if (!stack.children.length) stack.remove(); }, 4200);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = (options.method || 'GET').toUpperCase();
  const isForm = options.body instanceof FormData;
  if (options.body !== undefined && !isForm && typeof options.body !== 'string') {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  if (!['GET','HEAD','OPTIONS'].includes(method) && csrfToken && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(path, { ...options, method, headers, credentials:'same-origin' });
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(payload?.detail || `Request failed (${response.status})`);
    error.status = response.status; error.data = payload; throw error;
  }
  return payload;
}

async function download(path, filename) {
  const headers = new Headers();
  if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(path, { credentials:'same-origin', headers });
  if (!response.ok) {
    let text = `Download failed (${response.status})`;
    try { const obj = await response.json(); text = obj.detail || text; } catch (_) {}
    throw new Error(text);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return response.headers;
}

function pageHeading(eyebrow, title, subtitle, actions = '') {
  return `<div class="page-heading"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</div>`;
}
function emptyBlock(icon, title, text) {
  return `<div class="empty"><div class="empty-icon">${icon}</div><strong>${esc(title)}</strong><div>${esc(text)}</div></div>`;
}
function badge(status) {
  const s = String(status || '').toUpperCase();
  const cls = ['CHECKED IN','ISSUED','ACTIVE','OWNER','ADMIN'].includes(s) ? 'badge-green' : ['REVOKED','NOT CHECKED IN','INVALID_TICKET','TICKET_REVOKED'].includes(s) ? 'badge-red' : ['ALREADY_CHECKED_IN','VIEWER','CHECKIN_STAFF'].includes(s) ? 'badge-amber' : 'badge-neutral';
  return `<span class="badge ${cls}">${esc(status || '—')}</span>`;
}

function renderLogin(message = '') {
  stopScanner();
  const isScanner = isScannerOnlyMode || import.meta.env.VITE_SCANNER_ONLY === 'true';
  app.innerHTML = `
  <div class="login-shell">
    <section class="login-art">
      <div class="brand"><div class="brand-mark">C</div><div><div class="brand-name">CONFLUENCE</div><div class="brand-sub">${isScanner ? 'Gate Scanner' : 'Event operations'}</div></div></div>
      <div class="login-copy">
        <div class="eyebrow">${isScanner ? 'GATE ENTRANCE · ATTENDANCE VERIFICATION' : 'Admission control · One source of truth'}</div>
        <h1>${isScanner ? 'Fast, accurate<br>gate entry.' : 'Every guest.<br>One smooth entry.'}</h1>
        <p>${isScanner ? 'Sign in with your assigned staff or volunteer credentials to start scanning tickets and recording live attendance.' : 'Generate personalized QR tickets, keep check-in accurate across scanners, and leave the venue with a clean attendance report.'}</p>
      </div>
      <div class="login-meta">INTERNAL EVENT OPERATIONS · AUTHORIZED STAFF ONLY</div>
    </section>
    <section class="login-panel">
      <form id="loginForm" class="login-card">
        <div class="eyebrow">${isScanner ? 'GATE STAFF SIGN IN' : 'STAFF SIGN IN'}</div>
        <h2>Welcome back</h2>
        <p>${isScanner ? 'Sign in with your staff account to start scanning.' : 'Sign in with the account created by the event owner. Attendees do not need an account.'}</p>
        ${message ? `<div class="notice notice-error">${esc(message)}</div>` : ''}
        <div class="field"><label for="loginEmail">Work email</label><input id="loginEmail" name="email" type="email" autocomplete="username" required placeholder="staff@confluence.local"></div>
        <div class="field"><label for="loginPassword">Password</label><input id="loginPassword" name="password" type="password" autocomplete="current-password" required placeholder="Enter your password"></div>
        <button class="button button-primary full" type="submit">Sign in securely <span>→</span></button>
        <div class="footer-note">Access attempts are rate limited. Contact the event organizer if your account is inactive or you need access.</div>
      </form>
    </section>
  </div>`;

  document.querySelector('#loginForm').addEventListener('submit', async e => {
    e.preventDefault();
    const button = e.currentTarget.querySelector('button'); button.disabled = true; button.textContent = 'Signing in…';
    try {
      await api('/api/auth/login', { method:'POST', body:{ email: e.currentTarget.email.value, password: e.currentTarget.password.value } });
      const me = await api('/api/auth/me');
      csrfToken = me.csrfToken;
      sessionUser = me.user;
      isScannerOnlyMode = Boolean(me.scannerOnly || import.meta.env.VITE_SCANNER_ONLY === 'true');
      await renderApp();
    } catch (err) { renderLogin(err.message); }
  });
}

async function renderApp() {
  stopScanner();
  const me = await api('/api/auth/me');
  if (!me.authenticated) { csrfToken = ''; sessionUser = null; renderLogin(); return; }
  csrfToken = me.csrfToken; sessionUser = me.user;
  isScannerOnlyMode = Boolean(me.scannerOnly || import.meta.env.VITE_SCANNER_ONLY === 'true');

  // Scanner-only mode: Dedicated mobile layout with zero administrative clutter
  if (isScannerOnlyMode) {
    activePage = 'scanner';
    app.innerHTML = `<div class="scanner-shell">
      <header class="scanner-header">
        <div class="brand">
          <div class="brand-mark">C</div>
          <div>
            <div class="brand-name">CONFLUENCE</div>
            <div class="brand-sub">Gate Scanner</div>
          </div>
        </div>
        <div class="scanner-header-right">
          <div class="status-pill"><span class="status-dot"></span> Gate Online</div>
          <div class="avatar" title="${esc(sessionUser.email)}">${esc(initials(sessionUser.email))}</div>
          <button class="button button-small button-ghost" id="logoutBtn">Sign out</button>
        </div>
      </header>
      <main class="scanner-container" id="pageContent"><div class="empty">Starting scanner…</div></main>
    </div>`;

    document.querySelector('#logoutBtn')?.addEventListener('click', async () => {
      try {
        await api('/api/auth/logout', { method: 'POST', body: {} });
        csrfToken = ''; sessionUser = null; renderLogin();
      } catch (err) { toast(err.message, 'error'); }
    });

    await renderScanner(document.querySelector('#pageContent'));
    return;
  }

  // Full Organizer desktop mode (for local event management)
  if (sessionUser.role === 'CHECKIN_STAFF' && ['import','template','generate','report','staff','attendees'].includes(activePage)) activePage = 'scanner';
  if (sessionUser.role === 'VIEWER' && ['import','template','generate','scanner','staff'].includes(activePage)) activePage = 'attendees';
  const allowedNav = NAV.filter(n => {
    if (['OWNER','ADMIN'].includes(sessionUser.role)) return true;
    if (sessionUser.role === 'CHECKIN_STAFF') return ['dashboard','scanner','account'].includes(n.id);
    return ['dashboard','attendees','report','account'].includes(n.id);
  });
  app.innerHTML = `<div class="layout">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark">C</div><div><div class="brand-name">CONFLUENCE</div><div class="brand-sub">Event operations</div></div></div>
      <div class="event-chip"><small>Current event</small><strong>Confluence</strong><span>Attendee ticketing & check-in</span></div>
      <nav>${['EVENT','TICKETS','ENTRY','REPORTS','SETTINGS'].map(group => {
        const items = allowedNav.filter(n=>n.group===group); if (!items.length) return '';
        return `<div class="nav-label">${group}</div><div class="nav-list">${items.map(n=>`<button class="nav-item ${activePage===n.id?'active':''}" data-page="${n.id}"><span class="nav-icon">${n.icon}</span><span class="nav-text">${n.label}</span></button>`).join('')}</div>`;
      }).join('')}</nav>
      <div class="sidebar-bottom"><div class="user-mini"><div class="avatar">${esc(initials(sessionUser.email))}</div><div><strong>${esc(sessionUser.email)}</strong><small>${esc(sessionUser.role.replaceAll('_',' '))}</small></div></div><button class="nav-item" id="logoutBtn"><span class="nav-icon">↪</span><span class="nav-text">Sign out</span></button></div>
    </aside>
    <main class="main"><header class="topbar"><div class="breadcrumb">Confluence <span style="color:#a4b2b7"> / </span> <strong>${esc(NAV.find(n=>n.id===activePage)?.label || 'Overview')}</strong></div><div class="top-actions"><div class="status-pill"><span class="status-dot"></span> Online check-in</div><div class="avatar">${esc(initials(sessionUser.email))}</div></div></header><section class="content" id="pageContent"><div class="empty">Loading workspace…</div></section></main>
  </div>`;
  document.querySelectorAll('[data-page]').forEach(btn => btn.addEventListener('click', async () => { activePage = btn.dataset.page; await renderApp(); }));
  document.querySelector('#logoutBtn')?.addEventListener('click', async () => {
    try { await api('/api/auth/logout', {method:'POST', body:{}}); csrfToken=''; sessionUser=null; activePage='dashboard'; renderLogin(); }
    catch (err) { toast(err.message, 'error'); }
  });
  await renderPage(activePage);
}

async function renderPage(page) {
  const root = document.querySelector('#pageContent'); if (!root) return;
  root.innerHTML = '<div class="empty">Loading…</div>';
  try {
    if (page === 'dashboard') await renderDashboard(root);
    else if (page === 'import') renderImport(root);
    else if (page === 'template') await renderTemplate(root);
    else if (page === 'generate') renderGenerate(root);
    else if (page === 'scanner') renderScanner(root);
    else if (page === 'attendees') await renderAttendees(root);
    else if (page === 'report') renderReport(root);
    else if (page === 'staff') await renderStaff(root);
    else if (page === 'account') renderAccount(root);
    else await renderDashboard(root);
  } catch (err) {
    root.innerHTML = `<div class="notice notice-error">${esc(err.message)} <button class="button button-small button-ghost" id="retryPage">Retry</button></div>`;
    document.querySelector('#retryPage')?.addEventListener('click', () => renderPage(page));
  }
}

async function renderDashboard(root) {
  const d = await api('/api/dashboard');
  const recent = d.recentScans?.length ? `<div class="table-wrap"><table><thead><tr><th>Result</th><th>Time</th></tr></thead><tbody>${d.recentScans.map(x=>`<tr><td>${badge(x.result.replaceAll('_',' '))}</td><td>${esc(fmtDate(x.time))}</td></tr>`).join('')}</tbody></table></div>` : emptyBlock('⌗','No scans yet','Successful and rejected scan attempts will appear here.');
  const dashboardActions = ['OWNER','ADMIN','CHECKIN_STAFF'].includes(sessionUser.role) ? `<button class="button button-primary" id="openScanner">⌗ &nbsp; Open scanner</button>` : `<button class="button button-primary" id="openReport">⇩ &nbsp; Attendance report</button>`;
  root.innerHTML = `${pageHeading('EVENT OVERVIEW','Confluence operations','Manage your student list, prepare the ticket artwork, and keep entry moving with a shared live attendance record.',dashboardActions)}
    <div class="metric-grid">
      <div class="metric"><div class="metric-head">REGISTERED STUDENTS <span class="metric-icon">♙</span></div><div class="metric-value">${fmtNumber(d.registered)}</div><div class="metric-sub">Imported from your Excel roster</div></div>
      <div class="metric"><div class="metric-head">TICKETS ISSUED <span class="metric-icon">▤</span></div><div class="metric-value">${fmtNumber(d.ticketsIssued)}</div><div class="metric-sub">Valid tickets currently issued</div></div>
      <div class="metric"><div class="metric-head">CHECKED IN <span class="metric-icon">✓</span></div><div class="metric-value">${fmtNumber(d.checkedIn)}</div><div class="metric-sub">${d.attendanceRate}% of registered students</div><div class="progress"><div style="width:${Math.min(100,d.attendanceRate)}%"></div></div></div>
      <div class="metric"><div class="metric-head">NOT CHECKED IN <span class="metric-icon">◷</span></div><div class="metric-value">${fmtNumber(d.notCheckedIn)}</div><div class="metric-sub">Includes students not yet arrived</div></div>
    </div>
    <div class="grid-two"><section class="card"><div class="card-head"><div><h3>Recent scan activity</h3><p>Most recent ticket validation attempts</p></div><button class="button button-small button-ghost" id="refreshOverview">↻ Refresh</button></div>${recent}</section>
    ${['OWNER','ADMIN'].includes(sessionUser.role) ? `<section class="card"><div class="card-head"><div><h3>Event setup</h3><p>Recommended order before doors open</p></div></div><div class="card-body"><div class="quick-actions">
      <button class="quick-action" data-go="import"><span class="qa-icon">⇧</span><strong>1. Import roster</strong><span>Review the Excel file and resolve duplicates.</span></button>
      <button class="quick-action" data-go="template"><span class="qa-icon">▧</span><strong>2. Place ticket fields</strong><span>Upload artwork and position QR, name, year, phone.</span></button>
      <button class="quick-action" data-go="generate"><span class="qa-icon">▤</span><strong>3. Generate tickets</strong><span>Download individual PNG tickets in a ZIP.</span></button>
      <button class="quick-action" data-go="report"><span class="qa-icon">⇩</span><strong>4. Export attendance</strong><span>Download the final Excel attendance sheet.</span></button>
    </div><div class="notice">Ticket generation creates the same credentials that the scanner validates. Keep the ZIP private because each QR represents an admission credential.</div></div></section>` : sessionUser.role === 'CHECKIN_STAFF' ? `<section class="card"><div class="card-head"><div><h3>Entry operations</h3><p>Use the controlled online check-in path</p></div></div><div class="card-body"><button class="button button-primary full" id="openScannerCard">Open scanner →</button><div class="notice">Only a confirmed green response indicates that the database recorded the admission. A network error is not an approval.</div></div></section>` : `<section class="card"><div class="card-head"><div><h3>Attendance reporting</h3><p>Review the current status or export the roster</p></div></div><div class="card-body"><button class="button button-primary full" id="openReportCard">Open attendance report →</button><div class="notice">The report includes every imported student, including those who have not checked in.</div></div></section>`}</div>`;
  document.querySelector('#openScanner')?.addEventListener('click', () => go('scanner'));
  document.querySelector('#openScannerCard')?.addEventListener('click', () => go('scanner'));
  document.querySelector('#openReport')?.addEventListener('click', () => go('report'));
  document.querySelector('#openReportCard')?.addEventListener('click', () => go('report'));
  document.querySelector('#refreshOverview')?.addEventListener('click', () => renderPage('dashboard'));
  document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.go)));
}
function go(page) { activePage = page; renderApp(); }

function renderImport(root) {
  importInspection = null; importPreview = null; selectedFile = null;
  root.innerHTML = `${pageHeading('ROSTER MANAGEMENT','Import students','Upload the final .xlsx roster, map the columns, review invalid or duplicate rows, and then commit the import.')}
    <div class="steps"><div class="step"><div class="step-num">1</div><div><strong>Choose Excel file</strong><span>Use the first worksheet, with one student per row and a header row.</span></div></div><div class="step"><div class="step-num">2</div><div><strong>Map and preview</strong><span>Confirm which columns contain Name, Year, Email and Phone.</span></div></div><div class="step"><div class="step-num">3</div><div><strong>Commit verified rows</strong><span>Valid rows are added; invalid and duplicate rows are skipped and counted.</span></div></div></div>
    <section class="card"><div class="card-head"><div><h3>Excel roster</h3><p>Supported format: .xlsx · maximum 15 MB / 10,000 rows</p></div></div><div class="card-body">
      <div class="field"><label for="excelFile">Select student Excel file</label><input id="excelFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
      <div id="importArea"></div>
    </div></section>`;
  document.querySelector('#excelFile').addEventListener('change', async e => {
    selectedFile = e.target.files[0];
    if (!selectedFile) return;
    const area = document.querySelector('#importArea');
    area.innerHTML = '<div class="empty">Reading worksheet columns…</div>';
    try {
      const fd = new FormData(); fd.append('file', selectedFile);
      importInspection = await api('/api/imports/inspect', { method: 'POST', body: fd });
      renderMappingForm(area);
    } catch (err) { area.innerHTML = `<div class="notice notice-error">${esc(err.message)}</div>`; }
  });
}
function renderMappingForm(container) {
  const { headers, autoMapping, sampleRows, totalRows } = importInspection;
  const options = ['<option value="">(Skip column)</option>', ...headers.map(h => `<option value="${esc(h)}">${esc(h)}</option>`)].join('');
  container.innerHTML = `<div class="notice">Found <strong>${fmtNumber(totalRows)}</strong> data rows and <strong>${headers.length}</strong> headers. Confirm column assignment below:</div>
    <form id="mappingForm"><div class="mapping-grid">
      <div class="field"><label>Student Name *</label><select name="name" class="control-input" required>${options}</select></div>
      <div class="field"><label>Year / Batch</label><select name="year" class="control-input">${options}</select></div>
      <div class="field"><label>Email address</label><select name="email" class="control-input">${options}</select></div>
      <div class="field"><label>Phone number</label><select name="phone" class="control-input">${options}</select></div>
    </div>
    <div style="margin:16px 0;display:flex;gap:8px"><button class="button button-primary" type="submit">Preview import rows →</button></div></form>
    <div id="previewArea"></div>`;
  const f = container.querySelector('#mappingForm');
  for (const [k, v] of Object.entries(autoMapping || {})) { if (f[k] && v) f[k].value = v; }
  f.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = f.querySelector('button'); btn.disabled = true; btn.textContent = 'Analyzing rows…';
    const mapping = { name: f.name.value, year: f.year.value || null, email: f.email.value || null, phone: f.phone.value || null };
    const area = document.querySelector('#previewArea');
    area.innerHTML = '<div class="empty">Checking for duplicates and missing values…</div>';
    try {
      const fd = new FormData(); fd.append('file', selectedFile); fd.append('mapping', JSON.stringify(mapping));
      importPreview = await api('/api/imports/preview', { method: 'POST', body: fd });
      renderCommitPreview(area);
    } catch (err) { area.innerHTML = `<div class="notice notice-error">${esc(err.message)}</div>`; }
    finally { btn.disabled = false; btn.textContent = 'Preview import rows →'; }
  });
}
function renderCommitPreview(container) {
  const { summary, rows, previewId, truncated } = importPreview;
  container.innerHTML = `<div class="preview-summary">
    <div class="summary-chip" style="color:var(--green)">✓ ${fmtNumber(summary.valid)} valid to import</div>
    <div class="summary-chip" style="color:var(--amber)">⚠ ${fmtNumber(summary.duplicates)} duplicate rows</div>
    <div class="summary-chip" style="color:var(--red)">✕ ${fmtNumber(summary.invalid)} invalid rows</div>
    <div class="summary-chip">Total reviewed: ${fmtNumber(summary.total)}</div>
  </div>
  ${summary.valid === 0 ? '<div class="notice notice-error">No valid rows were found in this selection. Correct the mapping or the Excel file and try again.</div>' : ''}
  <div class="table-wrap" style="max-height:360px"><table><thead><tr><th>Row</th><th>Status</th><th>Name</th><th>Year</th><th>Email</th><th>Phone</th><th>Notes</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${r.row_number}</td><td>${badge(r.status)}</td><td><strong>${esc(r.name || '—')}</strong></td><td>${esc(r.year || '—')}</td><td>${esc(r.email || '—')}</td><td>${esc(r.phone || '—')}</td><td style="color:${r.errors.length?'var(--red)':'var(--muted)'};font-size:11px">${esc(r.errors.join(', ') || (r.status==='duplicate'?'Duplicate row or email':'OK'))}</td></tr>`).join('')}
  </tbody></table></div>
  ${truncated ? '<div class="footer-note">Showing first 200 rows in preview. All valid rows in the file will be imported.</div>' : ''}
  <div style="margin-top:18px"><button class="button button-primary" id="commitBtn" ${summary.valid===0?'disabled':''}>Commit ${fmtNumber(summary.valid)} valid rows</button></div>`;
  container.querySelector('#commitBtn')?.addEventListener('click', async e => {
    e.currentTarget.disabled = true; e.currentTarget.textContent = 'Saving roster…';
    try {
      const res = await api(`/api/imports/${previewId}/commit`, { method: 'POST' });
      toast(res.message, 'success');
      go('attendees');
    } catch (err) { toast(err.message, 'error'); e.currentTarget.disabled = false; e.currentTarget.textContent = 'Commit valid rows'; }
  });
}

async function renderTemplate(root) {
  root.innerHTML = `${pageHeading('TICKET DESIGN','Artwork & layout','Upload the organizer ticket image (PNG/JPG/WebP) and position the dynamic text and QR placeholder.')}
    <div class="template-layout"><section class="card"><div class="card-head"><div><h3>Visual layout editor</h3><p>Drag elements directly over the ticket artwork</p></div><input type="file" id="templateFileInput" accept="image/png,image/jpeg,image/webp" style="display:none"><button class="button button-small button-ghost" id="changeTemplate">Upload new image</button></div>
    <div class="card-body"><div class="template-stage-wrap"><div class="template-stage" id="templateStage"><div class="empty">Loading ticket design…</div></div></div></div></section>
    <aside class="template-controls"><h3>Layer properties</h3><p>Select a field on the canvas to inspect its position:</p>
      <div class="placement-options">
        <button class="placement-option" data-select="qr"><span>QR Code</span><small>Square</small></button>
        <button class="placement-option" data-select="name"><span>Attendee Name</span><small>Text</small></button>
        <button class="placement-option" data-select="year"><span>Year / Batch</span><small>Text</small></button>
        <button class="placement-option" data-select="phone"><span>Phone number</span><small>Text</small></button>
      </div>
      <div id="placementSummary"></div>
      <div style="margin-top:16px"><button class="button button-primary full" id="saveLayout">Save ticket layout</button></div>
      <div class="footer-note">Positions are normalized as percentages so generated tickets scale accurately to the original artwork dimensions.</div>
    </aside></div>`;
  document.querySelector('#changeTemplate').addEventListener('click', () => document.querySelector('#templateFileInput').click());
  document.querySelector('#templateFileInput').addEventListener('change', async e => {
    const file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append('file', file);
    try {
      templateInfo = await api('/api/template/image', { method: 'POST', body: fd });
      placements = templateInfo.placements;
      toast('Ticket image uploaded.', 'success');
      loadStage();
    } catch (err) { toast(err.message, 'error'); }
  });
  document.querySelectorAll('[data-select]').forEach(btn => btn.addEventListener('click', () => { selectedLayer = btn.dataset.select; updateLayers(); }));
  document.querySelector('#saveLayout').addEventListener('click', savePlacements);
  try {
    templateInfo = await api('/api/template');
    placements = templateInfo.placements;
    loadStage();
  } catch (err) {
    document.querySelector('#templateStage').innerHTML = `<div class="empty" style="padding:60px 20px"><div class="empty-icon">▧</div><strong>No ticket design uploaded yet</strong><p>Upload a PNG or JPG ticket design to begin positioning fields.</p><button class="button button-primary" onclick="document.querySelector('#templateFileInput').click()">Select ticket artwork</button></div>`;
  }
}
function loadStage() {
  const stage = document.querySelector('#templateStage');
  if (!stage || !templateInfo?.exists) return;
  stage.innerHTML = `<img id="ticketArtwork" src="${templateInfo.imageUrl}" alt="Ticket artwork">
    <div class="layer layer-qr" id="layer-qr" data-layer="qr">QR CODE</div>
    <div class="layer" id="layer-name" data-layer="name">Student Name</div>
    <div class="layer" id="layer-year" data-layer="year">Year / Batch</div>
    <div class="layer" id="layer-phone" data-layer="phone">+91 98765 00000</div>`;
  const img = stage.querySelector('#ticketArtwork');
  img.onload = () => { updateLayers(); installLayerDragging(); };
  window.addEventListener('resize', updateLayers);
}
function updateLayers() {
  if (!placements || !templateInfo) return;
  const stage = document.querySelector('#templateStage'); if (!stage) return;
  const img = document.querySelector('#ticketArtwork'); if (!img?.naturalWidth) return;
  const sw = stage.clientWidth;
  for (const key of ['qr','name','year','phone']) {
    const el = document.querySelector(`#layer-${key}`); if (!el) continue;
    const cfg = placements[key]; el.style.left = `${cfg.x*100}%`; el.style.top = `${cfg.y*100}%`;
    el.classList.toggle('selected', selectedLayer === key);
    if (key === 'qr') { el.style.width = `${cfg.size*100}%`; el.style.height = 'auto'; el.style.fontSize = `${Math.max(8, sw*0.012)}px`; }
    else { el.style.fontSize = `${Math.max(8, sw*cfg.scale)}px`; el.style.color = cfg.color; }
  }
  renderPlacementSummary();
}
function renderPlacementSummary() {
  const el = document.querySelector('#placementSummary'); if (!el || !placements) return;
  const p = placements[selectedLayer];
  el.innerHTML = `<div class="notice"><strong>Selected:</strong> ${esc(selectedLayer==='qr'?'QR code':selectedLayer==='name'?'Student name':selectedLayer==='year'?'Year':'Phone number')}<br>Position: ${Math.round(p.x*100)}% from left, ${Math.round(p.y*100)}% from top${selectedLayer==='qr'?`<br>QR size: ${Math.round(p.size*100)}% of image width`:''}</div>`;
}
function installLayerDragging() {
  const stage = document.querySelector('#templateStage'); if (!stage) return;
  stage.querySelectorAll('[data-layer]').forEach(el => el.addEventListener('pointerdown', e => {
    e.preventDefault(); const key = el.dataset.layer; selectedLayer = key;
    const rect = stage.getBoundingClientRect(); const xPx = placements[key].x*rect.width; const yPx = placements[key].y*rect.height;
    layerDrag = { key, startX: e.clientX, startY: e.clientY, offsetX: e.clientX-rect.left-xPx, offsetY: e.clientY-rect.top-yPx };
    el.setPointerCapture?.(e.pointerId); el.classList.add('selected');
  }));
  stage.onpointermove = (e) => {
    if (!layerDrag) return; const rect = stage.getBoundingClientRect(); const p = placements[layerDrag.key];
    let x = (e.clientX-rect.left-layerDrag.offsetX)/rect.width; let y = (e.clientY-rect.top-layerDrag.offsetY)/rect.height;
    const maxX = layerDrag.key==='qr'?1-p.size:0.96;
    const qrHeight = layerDrag.key==='qr'?p.size*templateInfo.width/templateInfo.height:0;
    const maxY = layerDrag.key==='qr'?1-qrHeight:0.98;
    p.x = Math.max(0, Math.min(maxX, x)); p.y = Math.max(0, Math.min(maxY, y));
    const el = document.querySelector(`#layer-${layerDrag.key}`); el.style.left = `${p.x*100}%`; el.style.top = `${p.y*100}%`; renderPlacementSummary();
  };
  stage.onpointerup = () => { layerDrag = null; updateLayers(); };
  stage.onpointercancel = () => { layerDrag = null; updateLayers(); };
}
async function savePlacements() {
  if (!placements) return toast('Upload a ticket design first.', 'error');
  try { const result = await api('/api/template/placements', { method: 'PUT', body: placements }); placements = result.placements; toast('Ticket layout saved.', 'success'); updateLayers(); }
  catch (e) { toast(e.message, 'error'); }
}

function renderGenerate(root) {
  root.innerHTML = `${pageHeading('TICKET ISSUANCE','Generate ticket ZIP','Create one personalized PNG for every imported student. Existing QR credentials are reused if you download the ZIP again.')}
    <div class="steps"><div class="step"><div class="step-num">✓</div><div><strong>Roster is imported</strong><span>Each student needs a row in the attendee list.</span></div></div><div class="step"><div class="step-num">✓</div><div><strong>Artwork layout is saved</strong><span>QR, name, year and phone positions have been reviewed.</span></div></div><div class="step"><div class="step-num">3</div><div><strong>Download ZIP</strong><span>Personalized images plus a small ticket manifest.</span></div></div></div>
    <section class="card"><div class="card-head"><div><h3>Generate Confluence tickets</h3><p>Output: individual PNG images in a ZIP archive</p></div>${badge('PNG + ZIP')}</div><div class="card-body"><div class="notice notice-warning">Treat the ZIP as sensitive. Anyone holding a valid QR ticket may be able to present it at the entrance. Share ticket images only with the intended attendee.</div><button class="button button-primary" id="generateZip">Generate and download tickets <span>↓</span></button><div id="generateStatus"></div><div class="footer-note">The ZIP contains one PNG per active ticket and ticket_manifest.csv. QR credentials are saved in the database before files are rendered, so a retry can reuse the same credentials.</div></div></section>`;
  document.querySelector('#generateZip').addEventListener('click', async e => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = 'Generating ticket images…';
    try { const headers = await download('/api/tickets/generate', 'Confluence-tickets.zip'); const count = headers.get('x-tickets-included') || '—'; document.querySelector('#generateStatus').innerHTML = `<div class="notice notice-success">Download started. Tickets included: <strong>${esc(count)}</strong>. Open the ZIP and confirm a few random tickets before distribution.</div>`; toast('Ticket ZIP generated.', 'success'); }
    catch (err) { document.querySelector('#generateStatus').innerHTML = `<div class="notice notice-error">${esc(err.message)}</div>`; toast(err.message, 'error'); }
    finally { btn.disabled = false; btn.innerHTML = 'Generate and download tickets <span>↓</span>'; }
  });
}

function renderScanner(root) {
  stopScanner(); scannerBusy = false;
  root.innerHTML = `${pageHeading('GATE OPERATIONS','Scan attendance','Use an authorized staff account on each device. A green result appears only after the server commits the check-in.')}
    <div class="scanner-layout">
      <section class="scanner-view">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div class="eyebrow" style="color:#83d0c8">LIVE ADMISSION</div>
          <div style="display:flex;gap:6px">
            <button class="button button-small button-ghost" id="toggleSound" style="background:rgba(255,255,255,.1);color:white;border-color:transparent" title="Toggle audio chime">${soundEnabled ? '🔊 Sound' : '🔇 Muted'}</button>
            <button class="button button-small button-ghost" id="flipCamera" style="background:rgba(255,255,255,.1);color:white;border-color:transparent" title="Switch camera">🔄 Flip</button>
          </div>
        </div>
        <h3>Ticket scanner</h3>
        <p>Allow camera access and hold the QR code inside the frame. Keep the phone connected to the internet while scanning.</p>
        <div class="video-box" id="videoBox">
          <div class="video-placeholder" id="videoPlaceholder"><div style="font-size:34px;margin-bottom:8px">⌗</div>Camera is off.<br>Start the scanner when you're ready.</div>
          <video id="scannerVideo" muted playsinline class="sr-only"></video>
          <div class="scanner-reticle" id="scannerReticle" style="display:none"></div>
        </div>
        <div class="scanner-actions">
          <button class="button button-primary" id="startCamera">Start camera</button>
          <button class="button button-ghost" id="stopCamera" disabled>Stop</button>
        </div>
        <form id="manualScanForm" class="manual-scan-form">
          <input id="manualToken" class="manual-scan-input" placeholder="Manual ticket QR or token code…" required>
          <button class="button button-secondary button-small" type="submit">Verify code</button>
        </form>
        <div class="footer-note" style="color:#9bb0b9">If the camera fails, check HTTPS and browser camera permissions. Never approve a ticket when the server result is unknown.</div>
      </section>
      <section>
        <div id="scanResult" class="scan-result pending">
          <span class="badge badge-neutral">READY</span>
          <h3>Ready to scan</h3>
          <p>The attendee's name and year will appear after the server validates the ticket.</p>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>Last confirmed entries</h3><p>Most recent committed attendance</p></div><button class="button button-small button-ghost" id="refreshRecent">Refresh</button></div>
          <div id="recentCheckins">Loading…</div>
        </div>
      </section>
    </div>`;

  document.querySelector('#startCamera').addEventListener('click', startScanner);
  document.querySelector('#stopCamera').addEventListener('click', stopScanner);
  document.querySelector('#refreshRecent').addEventListener('click', loadRecentCheckins);

  document.querySelector('#toggleSound')?.addEventListener('click', () => {
    soundEnabled = !soundEnabled;
    localStorage.setItem('confluence_sound', soundEnabled ? 'true' : 'false');
    document.querySelector('#toggleSound').textContent = soundEnabled ? '🔊 Sound' : '🔇 Muted';
    toast(soundEnabled ? 'Audio chime enabled.' : 'Audio chime muted.', 'info');
  });

  document.querySelector('#flipCamera')?.addEventListener('click', async () => {
    currentCamera = currentCamera === 'environment' ? 'user' : 'environment';
    if (scannerInstance) {
      await stopScanner(false);
      await startScanner();
    }
    toast(`Switched to ${currentCamera === 'environment' ? 'rear' : 'front'} camera.`, 'info');
  });

  document.querySelector('#manualScanForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    const input = document.querySelector('#manualToken');
    const val = input.value.trim();
    if (!val) return;
    input.value = '';
    await submitScan(val, crypto.randomUUID());
  });

  loadRecentCheckins();
}

async function startScanner() {
  if (scannerInstance) return;
  const video = document.querySelector('#scannerVideo');
  const status = document.querySelector('#videoPlaceholder');
  const reticle = document.querySelector('#scannerReticle');
  if (!video) return;
  document.querySelector('#startCamera').disabled = true;
  document.querySelector('#startCamera').textContent = 'Opening camera…';
  try {
    scannerInstance = new QrScanner(video, async result => {
      if (scannerBusy) return;
      scannerBusy = true;
      const raw = typeof result === 'string' ? result : result.data;
      await stopScanner(false);
      await submitScan(raw, crypto.randomUUID());
      document.querySelector('#startCamera').disabled = false;
      document.querySelector('#startCamera').textContent = 'Scan next ticket';
      scannerBusy = false;
    }, { preferredCamera: currentCamera, highlightScanRegion: true, highlightCodeOutline: true, returnDetailedScanResult: true, maxScansPerSecond: 8 });

    await scannerInstance.start();
    video.classList.remove('sr-only');
    status?.classList.add('sr-only');
    if (reticle) reticle.style.display = 'block';
    document.querySelector('#stopCamera').disabled = false;
    document.querySelector('#startCamera').textContent = 'Camera active';
  } catch (e) {
    scannerInstance = null;
    document.querySelector('#startCamera').disabled = false;
    document.querySelector('#startCamera').textContent = 'Try camera again';
    document.querySelector('#stopCamera').disabled = true;
    video.classList.add('sr-only');
    if (status) {
      status.classList.remove('sr-only');
      status.innerHTML = `Camera could not start.<br>${esc(e.message || 'Check camera permissions and HTTPS.')}`;
    }
    toast('Camera unavailable. Check browser permissions and ensure the site uses HTTPS.', 'error');
  }
}

async function stopScanner(reEnable = true) {
  if (scannerInstance) {
    try { await scannerInstance.stop(); scannerInstance.destroy(); } catch (_) {}
    scannerInstance = null;
  }
  const video = document.querySelector('#scannerVideo');
  if (video) {
    try { video.pause(); video.srcObject?.getTracks().forEach(t => t.stop()); } catch (_) {}
    video.srcObject = null;
    video.classList.add('sr-only');
  }
  const reticle = document.querySelector('#scannerReticle');
  if (reticle) reticle.style.display = 'none';

  const placeholder = document.querySelector('#videoPlaceholder');
  if (placeholder) {
    placeholder.classList.remove('sr-only');
    placeholder.innerHTML = '<div style="font-size:34px;margin-bottom:8px">⌗</div>Camera is off.<br>Start the scanner when you\'re ready.';
  }
  const start = document.querySelector('#startCamera');
  if (start && reEnable) { start.disabled = false; start.textContent = 'Start camera'; }
  const stop = document.querySelector('#stopCamera');
  if (stop) stop.disabled = true;
}

async function submitScan(raw, requestId) {
  const card = document.querySelector('#scanResult');
  if (card) {
    card.className = 'scan-result pending';
    card.innerHTML = '<span class="badge badge-amber">VERIFYING</span><h3>Checking ticket…</h3><p>Waiting for the central attendance database.</p>';
  }
  try {
    const data = await api('/api/check-ins', { method: 'POST', body: { qrToken: raw, clientScanId: requestId } });
    showScanResult(data, 'success');
    playChime('success');
    triggerHaptic('success');
    toast(`${data.attendee?.name || 'Attendee'} checked in.`, 'success');
  } catch (err) {
    playChime('error');
    triggerHaptic('error');
    if (err.data?.result) {
      showScanResult(err.data, err.data.result === 'ALREADY_CHECKED_IN' ? 'error' : 'error');
      toast(err.data.message || err.message, 'error');
    } else {
      showScanResult({
        result: 'NOT_VERIFIED',
        message: 'The server response was not received. Do not admit based on this scan. Check connectivity and scan again.'
      }, 'pending');
      toast('Scan outcome could not be confirmed. Do not admit until verified.', 'error');
    }
  }
  await loadRecentCheckins();
}

function showScanResult(data, kind) {
  const card = document.querySelector('#scanResult'); if (!card) return;
  const status = data.result || 'NOT_VERIFIED';
  const heading = status === 'CHECKED_IN' ? 'ENTRY APPROVED' : status === 'ALREADY_CHECKED_IN' ? 'ALREADY CHECKED IN' : status === 'TICKET_REVOKED' ? 'TICKET REVOKED' : status === 'INVALID_TICKET' ? 'INVALID TICKET' : 'NOT VERIFIED';
  const cls = kind === 'success' ? 'success' : kind === 'pending' ? 'pending' : 'error';
  card.className = `scan-result ${cls}`;
  card.innerHTML = `<span class="badge ${kind==='success'?'badge-green':kind==='pending'?'badge-amber':'badge-red'}">${esc(status.replaceAll('_',' '))}</span>
    <h3>${heading}</h3>
    <p><strong>${esc(data.attendee?.name || '')}</strong> ${data.attendee?.year ? `· ${esc(data.attendee.year)}` : ''}<br>
    ${esc(data.message || '')} ${data.checkedInAt ? `<br>Recorded at ${esc(fmtDate(data.checkedInAt))}` : ''}<br>
    ${data.ticketNumber ? `Ticket: <strong>${esc(data.ticketNumber)}</strong>` : ''}</p>`;
}

async function loadRecentCheckins() {
  const area = document.querySelector('#recentCheckins'); if (!area) return;
  try {
    const d = await api('/api/check-ins/recent');
    area.innerHTML = d.rows.length ? `<div class="table-wrap"><table style="min-width:300px"><thead><tr><th>Attendee</th><th>Time</th></tr></thead><tbody>${d.rows.slice(0, 8).map(r => `<tr><td><div class="td-primary">${esc(r.name)}</div><div class="td-muted">${esc(r.ticketNumber)}</div></td><td>${esc(fmtDate(r.checkedInAt))}</td></tr>`).join('')}</tbody></table></div>` : emptyBlock('✓', 'No confirmed entries', 'Successful scans appear here.');
  } catch (e) { area.innerHTML = `<div class="notice notice-error">${esc(e.message)}</div>`; }
}

async function renderAttendees(root) {
  root.innerHTML = `${pageHeading('ATTENDEE DIRECTORY','Student roster','Search names, ticket IDs, email addresses or phone numbers. Check-in status reflects the latest saved database state.')}
    <section class="card"><div class="toolbar"><div class="toolbar-left"><input id="attendeeSearch" class="search" placeholder="Search student or ticket…"><select id="attendeeStatus" class="control-input" style="width:auto;min-height:38px;font-size:12px"><option value="all">All students</option><option value="checked_in">Checked in</option><option value="not_checked_in">Not checked in</option></select></div><div class="toolbar-right"><button class="button button-small button-ghost" id="refreshAttendees">↻ Refresh</button><button class="button button-small button-primary" id="exportAttendees">Export Excel</button></div></div><div id="attendeeTable">Loading students…</div></section>`;
  const load = async () => {
    const q = document.querySelector('#attendeeSearch').value; const status = document.querySelector('#attendeeStatus').value;
    const d = await api(`/api/attendees?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}`);
    document.querySelector('#attendeeTable').innerHTML = d.rows.length ? `<div class="table-wrap"><table><thead><tr><th>Student</th><th>Contact</th><th>Ticket</th><th>Attendance</th><th>Checked in at</th><th>Action</th></tr></thead><tbody>${d.rows.map(r => `<tr><td><div class="td-primary">${esc(r.name)}</div><div class="td-muted">${esc(r.year || 'Year not provided')}</div></td><td>${esc(r.email || '—')}<div class="td-muted">${esc(r.phone || '—')}</div></td><td>${r.ticketNumber ? `<div class="td-primary">${esc(r.ticketNumber)}</div>${badge(r.ticketStatus)}` : badge('NOT ISSUED')}</td><td>${badge(r.attendanceStatus)}</td><td>${esc(fmtDate(r.checkedInAt))}</td><td>${r.ticketStatus === 'ISSUED' && ['OWNER','ADMIN'].includes(sessionUser.role) ? `<button class="button button-small button-danger" data-revoke="${esc(r.ticketId)}" data-name="${esc(r.name)}">Revoke</button>` : '—'}</td></tr>`).join('')}</tbody></table></div><div style="padding:12px 16px;color:var(--muted);font-size:11px">Showing ${fmtNumber(d.count)} matching records (maximum 1,000 per query).</div>` : emptyBlock('♙', 'No students found', 'Import your Excel roster or change the search/filter.');
    document.querySelectorAll('[data-revoke]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm(`Revoke ticket for ${b.dataset.name}? The QR will no longer be accepted.`)) return;
      try { await api(`/api/tickets/${b.dataset.revoke}/revoke`, { method: 'POST', body: {} }); toast('Ticket revoked.', 'success'); await load(); }
      catch (e) { toast(e.message, 'error'); }
    }));
  };
  document.querySelector('#attendeeSearch').addEventListener('input', debounce(() => load().catch(e => toast(e.message, 'error')), 250));
  document.querySelector('#attendeeStatus').addEventListener('change', () => load().catch(e => toast(e.message, 'error')));
  document.querySelector('#refreshAttendees').addEventListener('click', () => load().catch(e => toast(e.message, 'error')));
  document.querySelector('#exportAttendees').addEventListener('click', () => download('/api/reports/attendance.xlsx', 'Confluence-attendance.xlsx').catch(e => toast(e.message, 'error')));
  await load();
}
function debounce(fn, wait) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); }; }

function renderReport(root) {
  root.innerHTML = `${pageHeading('REPORTING','Attendance report','Download an Excel report containing every imported student, their year, email, phone and current attendance status.')}
    <section class="card"><div class="card-head"><div><h3>Confluence attendance workbook</h3><p>Excel (.xlsx) · one row per registered student</p></div>${badge('XLSX')}</div><div class="card-body"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:18px"><div class="metric" style="padding:15px"><div class="metric-head">INCLUDED</div><div class="metric-value" style="font-size:23px">All students</div><div class="metric-sub">Including no-shows</div></div><div class="metric" style="padding:15px"><div class="metric-head">STATUS SOURCE</div><div class="metric-value" style="font-size:23px">Database</div><div class="metric-sub">Latest committed check-ins</div></div><div class="metric" style="padding:15px"><div class="metric-head">COLUMNS</div><div class="metric-value" style="font-size:23px">5</div><div class="metric-sub">Name, Year, Email, Phone, Attendance Status</div></div></div><button class="button button-primary" id="downloadReport">Download attendance Excel ↓</button><div class="notice">The report is generated when you click download, not cached from the dashboard. Anyone marked NOT CHECKED IN has no active check-in record at the time of export.</div><div id="reportStatus"></div></div></section>`;
  document.querySelector('#downloadReport').addEventListener('click', async () => {
    const b = document.querySelector('#downloadReport'); b.disabled = true; b.textContent = 'Preparing workbook…';
    try { await download('/api/reports/attendance.xlsx', 'Confluence-attendance.xlsx'); document.querySelector('#reportStatus').innerHTML = '<div class="notice notice-success">Attendance workbook download started.</div>'; toast('Attendance report downloaded.', 'success'); }
    catch (e) { toast(e.message, 'error'); }
    finally { b.disabled = false; b.textContent = 'Download attendance Excel ↓'; }
  });
}

function renderAccount(root) {
  root.innerHTML = `${pageHeading('ACCOUNT SECURITY','My account','Manage your own credentials. Staff accounts are individual; do not share scanner sessions with other volunteers.')}
    <section class="card"><div class="card-head"><div><h3>Signed-in account</h3><p>Current identity and assigned role</p></div></div><div class="card-body"><div class="notice"><strong>${esc(sessionUser.email)}</strong><br>Role: ${esc(sessionUser.role.replaceAll('_',' '))}</div><form id="accountPasswordForm" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;align-items:end"><div class="field" style="margin:0"><label for="accountOldPassword">Current password</label><input id="accountOldPassword" type="password" required autocomplete="current-password"></div><div class="field" style="margin:0"><label for="accountNewPassword">New password</label><input id="accountNewPassword" type="password" minlength="12" required autocomplete="new-password" placeholder="12+ characters"></div><button class="button button-primary" type="submit">Update password</button></form><div class="footer-note">Use a unique password of at least 12 characters. If an account is compromised, ask an administrator to deactivate it immediately.</div></div></section>`;
  document.querySelector('#accountPasswordForm').addEventListener('submit', async e => {
    e.preventDefault(); const b = e.currentTarget.querySelector('button'); b.disabled = true;
    try { const result = await api('/api/auth/change-password', { method: 'POST', body: { oldPassword: document.querySelector('#accountOldPassword').value, newPassword: document.querySelector('#accountNewPassword').value } }); e.currentTarget.reset(); toast(result.message || 'Password updated.', 'success'); }
    catch (err) { toast(err.message, 'error'); }
    finally { b.disabled = false; }
  });
}

async function renderStaff(root) {
  root.innerHTML = `${pageHeading('ACCESS CONTROL','Staff & permissions','Create individual accounts for volunteers and organizers. Never share one scanner login across multiple volunteers.')}
    <section class="card"><div class="card-head"><div><h3>Create staff account</h3><p>Passwords must contain at least 12 characters.</p></div></div><div class="card-body"><form id="staffForm" class="staff-form"><div class="field" style="margin:0"><label for="staffEmail">Staff email</label><input id="staffEmail" type="email" required placeholder="volunteer@college.edu"></div><div class="field" style="margin:0"><label for="staffRole">Role</label><select id="staffRole"><option value="CHECKIN_STAFF">Check-in staff</option><option value="VIEWER">Read-only viewer</option>${sessionUser.role==='OWNER'?'<option value="ADMIN">Admin</option>':''}</select></div><div class="field" style="margin:0"><label for="staffPassword">Temporary password</label><input id="staffPassword" type="password" minlength="12" required autocomplete="new-password" placeholder="12+ characters"></div><button class="button button-primary" type="submit">Create account</button></form><div id="staffStatus"></div></div></section>
    <section class="card" style="margin-top:16px"><div class="card-head"><div><h3>Authorized staff</h3><p>Inactive users cannot authenticate or scan tickets.</p></div><button class="button button-small button-ghost" id="refreshStaff">Refresh</button></div><div id="staffList">Loading accounts…</div></section>`;
  const load = async () => {
    const d = await api('/api/staff');
    document.querySelector('#staffList').innerHTML = `<div class="table-wrap"><table style="min-width:500px"><thead><tr><th>Staff</th><th>Role</th><th>Status</th><th>Added</th><th>Action</th></tr></thead><tbody>${d.rows.map(r => `<tr><td><div class="td-primary">${esc(r.email)}</div></td><td>${badge(r.role.replaceAll('_',' '))}</td><td>${badge(r.active?'ACTIVE':'INACTIVE')}</td><td>${esc(fmtDate(r.createdAt))}</td><td>${r.active&&r.id!==sessionUser.id&&!(r.role==='OWNER'&&sessionUser.role!=='OWNER')?`<button class="button button-small button-danger" data-deactivate="${esc(r.id)}" data-email="${esc(r.email)}">Deactivate</button>`:'—'}</td></tr>`).join('')}</tbody></table></div>`;
    document.querySelectorAll('[data-deactivate]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm(`Deactivate ${b.dataset.email}?`)) return;
      try { await api(`/api/staff/${b.dataset.deactivate}/deactivate`, { method: 'POST', body: {} }); toast('Staff account deactivated.', 'success'); load(); }
      catch (e) { toast(e.message, 'error'); }
    }));
  };
  document.querySelector('#refreshStaff').addEventListener('click', () => load().catch(e => toast(e.message, 'error')));
  document.querySelector('#staffForm').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.currentTarget.querySelector('button'); button.disabled = true;
    try { await api('/api/staff', { method: 'POST', body: { email: document.querySelector('#staffEmail').value, password: document.querySelector('#staffPassword').value, role: document.querySelector('#staffRole').value } }); e.currentTarget.reset(); toast('Staff account created. Share credentials securely.', 'success'); await load(); }
    catch (err) { toast(err.message, 'error'); }
    finally { button.disabled = false; }
  });
  await load();
}

(async function boot() {
  try {
    const me = await api('/api/auth/me');
    isScannerOnlyMode = Boolean(me.scannerOnly || import.meta.env.VITE_SCANNER_ONLY === 'true');
    if (me.authenticated) {
      sessionUser = me.user;
      csrfToken = me.csrfToken;
      await renderApp();
    } else {
      renderLogin();
    }
  } catch (e) {
    app.innerHTML = `<div class="login-shell"><div class="login-panel"><div class="login-card"><div class="eyebrow">SERVICE STATUS</div><h2>Workspace unavailable</h2><p>${esc(e.message)} Make sure the backend is running and database migrations have completed.</p><button class="button button-primary" id="retryReload">Retry</button></div></div></div>`;
    document.querySelector('#retryReload')?.addEventListener('click', () => location.reload());
  }
})();
