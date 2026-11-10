import { createCatalog } from '../catalog.js';
import { loadRoomCatalog, EDITION_CHANGED } from './catalog.js';
import { createRoomSession, shareRoomUrl } from './session.js';

const $ = id => document.getElementById(id);
let storage = null;
try { storage = window.localStorage; } catch { /* Storage is optional. */ }
let requestedCode = '';
let activeFrameKey = '';
let historyKey = '';
let frameSerial = 0;

function openDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
function updateLocation(view, navigation) {
  if (navigation === 'none' || !view.site) return;
  const url = new URL(shareRoomUrl(location.href, view.code, view.edition));
  url.searchParams.set('step', view.position);
  const method = navigation === 'push' ? 'pushState' : 'replaceState';
  history[method](null, '', url);
}
function renderFrame(view) {
  const key = view.site ? `${view.code}:${view.edition}:${view.site.id}` : '';
  if (key === activeFrameKey) return;
  activeFrameKey = key;
  const serial = ++frameSerial;
  $('frame-host').replaceChildren(); // Unload previous game/audio; do not accumulate iframe history.
  if (!view.site) return;
  const frame = document.createElement('iframe');
  frame.title = `Game: ${view.site.name}`;
  frame.referrerPolicy = 'no-referrer';
  // Same-origin catalog projects must not be able to remove their own sandbox.
  const crossOrigin = new URL(view.site.url).origin !== location.origin;
  frame.setAttribute('sandbox', `allow-scripts allow-forms allow-pointer-lock${crossOrigin ? ' allow-same-origin' : ''}`);
  frame.allow = 'autoplay; fullscreen; gamepad';
  frame.allowFullscreen = true;
  frame.addEventListener('load', () => {
    if (serial === frameSerial && !session.state.error && !session.state.busy) $('room-status').hidden = true;
    // load is not proof of playability; the direct-open action is always available.
  });
  frame.src = view.site.url;
  $('frame-host').append(frame);
}
function renderHistory(view) {
  const key = JSON.stringify([view.code, view.position, view.history.map(entry => entry.position)]);
  if (key === historyKey) return;
  historyKey = key;
  const fragment = document.createDocumentFragment();
  for (const entry of [...view.history].reverse()) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'list-group-item list-group-item-action';
    button.setAttribute('aria-current', String(entry.position === view.position));
    if (entry.position === view.position) button.classList.add('list-group-item-light');
    const title = document.createElement('strong');
    title.textContent = `${entry.position + 1}. ${entry.name}`;
    const repo = document.createElement('small');
    repo.textContent = entry.repo;
    button.append(title, repo);
    button.addEventListener('click', () => {
      $('history-dialog').close();
      void session.go(entry.position); // Selecting history disables auto-rotation.
    });
    fragment.append(button);
  }
  $('history-list').replaceChildren(fragment);
}
function render(view, navigation) {
  updateLocation(view, navigation);
  $('game-title').textContent = view.site?.name || 'Play together, at your own pace';
  $('room-label').textContent = view.code
    ? `Room ${view.code} · ${view.position + 1} of ${view.total}${view.auto ? ' · Auto-rotate on' : ''}`
    : 'Same room code. Same game order.';
  $('active-room').textContent = `Room ${view.code}`;
  $('active-settings').hidden = !view.site;
  $('welcome').hidden = Boolean(view.site);
  $('frame-host').hidden = !view.site;
  $('game-actions').hidden = !view.site;
  $('star-repo').hidden = !view.site;
  $('auto-rotate').checked = view.auto;
  $('auto-rotate').disabled = !view.site || view.busy;
  for (const id of ['previous-game', 'next-game']) $(id).disabled = !view.site || view.busy;
  $('show-history').disabled = !view.history.length || view.busy;
  $('join-room').disabled = view.busy;
  $('room-status').hidden = !view.error && !view.busy;
  $('room-status').textContent = view.error || (view.busy ? 'Loading the room’s game…' : '');
  $('room-feedback').textContent = view.error || (view.busy ? 'Loading…' : '');
  $('current-edition').hidden = view.error !== EDITION_CHANGED;
  if (view.site) {
    $('open-directly').href = view.site.url;
    $('star-repo').href = view.site.repoUrl;
    const report = new URL('https://github.com/abhidya/theuselessvibe/issues/new');
    report.search = new URLSearchParams({ template: 'report-site.yml', title: `[Site report] ${view.site.repo}`, 'site-url': view.site.url, 'source-repository': view.site.repoUrl });
    $('report-game').href = report.href;
  }
  renderHistory(view);
  renderFrame(view);
}
const session = createRoomSession({
  storage,
  loadCatalog: edition => loadRoomCatalog({ adapter: createCatalog(), edition }),
  onChange: render,
});
async function join(code, options = {}) {
  requestedCode = code;
  $('share-link').hidden = true;
  $('share-link-label').hidden = true;
  await session.join(code, options);
  if (!session.state.error && session.state.site) $('room-dialog').close();
  else if (session.state.error) openDialog('room-dialog');
}
async function restoreUrl() {
  const url = new URL(location.href);
  const code = url.searchParams.get('room');
  if (!code) { session.leave(); openDialog('room-dialog'); return; }
  $('room-code').value = code;
  const raw = url.searchParams.get('step');
  const position = raw !== null && /^\d{1,10}$/.test(raw) ? Number(raw) : null;
  if (session.state.code === code && session.state.edition === url.searchParams.get('v') && position !== null) {
    await session.go(position, 'none');
  } else {
    await join(code, { edition: url.searchParams.get('v'), position, navigation: 'replace' });
  }
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => $(button.dataset.close).close());
}
for (const id of ['room-menu', 'welcome-join']) $(id).addEventListener('click', () => openDialog('room-dialog'));
$('more-menu').addEventListener('click', () => openDialog('more-dialog'));
$('show-history').addEventListener('click', () => openDialog('history-dialog'));
$('previous-game').addEventListener('click', () => void session.move(-1));
$('next-game').addEventListener('click', () => void session.move(1));
$('auto-rotate').addEventListener('change', event => void session.setAuto(event.target.checked));
$('join-form').addEventListener('submit', event => {
  event.preventDefault();
  void join($('room-code').value);
});
$('current-edition').addEventListener('click', () => void join(requestedCode));
$('leave-room').addEventListener('click', () => {
  session.leave();
  history.pushState(null, '', location.pathname);
  $('room-dialog').close();
});
$('share-room').addEventListener('click', async () => {
  const view = session.state;
  if (!view.site) return;
  const link = shareRoomUrl(location.href, view.code, view.edition);
  $('share-link').value = link;
  $('share-link').hidden = false;
  $('share-link-label').hidden = false;
  try {
    await navigator.clipboard.writeText(link);
    $('room-feedback').textContent = 'Room link copied. Auto-rotate stays off for anyone joining.';
  } catch {
    $('share-link').focus();
    $('share-link').select();
    $('room-feedback').textContent = 'Select and copy the room link.';
  }
});
window.addEventListener('popstate', () => void restoreUrl());
window.addEventListener('focus', () => void session.tick());
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) void session.tick();
});
const timer = setInterval(() => { if (!document.hidden) void session.tick(); }, 1000);
window.addEventListener('pagehide', () => { clearInterval(timer); void session.setAuto(false); });
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
render(session.state, 'none');
void restoreUrl();
