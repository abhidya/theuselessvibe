import { createTrail } from './trail.js';

const $ = id => document.getElementById(id);
const PROJECT = 'https://github.com/abhidya/theuselessvibe';
const state = {
  adapter: null, stats: null, query: '', loading: false, busy: false,
  request: 0, selectionRequest: 0, nextOffset: null, generation: null,
  total: 0, shown: 0, seen: new Set(), site: null,
};
let storage = null;
try { storage = window.localStorage; } catch { /* Storage is optional. */ }
const trail = createTrail(storage);
const count = new Intl.NumberFormat();
let searchTimer;

function sourceURL(site) {
  if (!site || typeof site.repo !== 'string' || !/^[a-z0-9-]+\/[a-z0-9._-]+$/i.test(site.repo)) return null;
  return `https://github.com/${site.repo}`;
}
function validSite(site) {
  try {
    const url = new URL(site.url);
    return typeof site.id === 'string' && sourceURL(site) && url.protocol === 'https:' &&
      !url.username && !url.password && !url.port &&
      url.hostname === `${site.repo.split('/')[0].toLowerCase()}.github.io`;
  } catch { return false; }
}
function reportURL(site) {
  const url = new URL(`${PROJECT}/issues/new`);
  url.search = new URLSearchParams({ template: 'report-site.yml', title: `[Site report] ${site.repo}`,
    'site-url': site.url, 'source-repository': sourceURL(site) });
  return url.href;
}
function external(label, href, className) {
  const link = document.createElement('a');
  link.textContent = label; link.href = href; link.className = className;
  link.target = '_blank'; link.rel = 'noopener noreferrer';
  return link;
}
function siteCard(site) {
  const column = document.createElement('div'); column.className = 'col';
  const card = document.createElement('article'); card.className = 'card site-card h-100';
  const body = document.createElement('div'); body.className = 'card-body d-flex flex-column';
  const title = document.createElement('h3'); title.className = 'card-title h5';
  const visit = external(site.name, site.url, 'card-site-link link-dark text-decoration-none');
  visit.setAttribute('aria-label', `Play ${site.name} here`);
  visit.addEventListener('click', event => {
    if (event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      event.preventDefault(); navigateToGame(site);
    }
  });
  title.append(visit);
  const author = document.createElement('p'); author.className = 'card-repo small text-body-secondary';
  author.textContent = site.repo;
  const description = document.createElement('p'); description.className = 'card-text small text-body-secondary';
  description.textContent = site.description || 'An overlooked project. Open it and explore.';
  const actions = document.createElement('div'); actions.className = 'mt-auto pt-2 d-flex flex-wrap gap-3 small';
  const play = document.createElement('button'); play.type = 'button'; play.className = 'btn btn-outline-danger btn-sm';
  play.textContent = 'Play →'; play.addEventListener('click', () => navigateToGame(site));
  actions.append(play, external('Source ↗', sourceURL(site), 'link-secondary align-self-center'));
  body.append(title, author, description, actions); card.append(body); column.append(card); return column;
}
function playerVisible() { return $('game-player').open; }
function updateControls() {
  $('random-button').disabled = !state.adapter || !state.total || state.loading || state.busy;
  $('random-button').setAttribute('aria-busy', String(state.busy));
  $('random-button-label').textContent = state.busy ? 'Choosing a game…' : state.loading ? 'Loading the collection…' :
    state.total ? `${count.format(state.total)} ${state.query ? 'matching ' : ''}links to explore.` : 'No matching games in this snapshot.';
  $('resume-button').hidden = !trail.current;
  $('player-previous').disabled = !trail.canPrevious;
  $('player-next').disabled = state.busy || (!trail.canForward && (!state.adapter || !state.total || state.loading));
  $('player-history').disabled = !trail.entries.length;
  $('clear-filters').hidden = !state.query;
  $('load-more').disabled = state.loading;
  $('site-grid').setAttribute('aria-busy', String(state.loading));
}
function directoryMessage(title, message, retry = false) {
  $('directory-state-title').textContent = title; $('directory-state-description').textContent = message;
  $('directory-state').hidden = false; $('retry-button').hidden = !retry;
}
function renderStats(stats) {
  state.stats = stats;
  $('eligible-count').textContent = count.format(stats.eligibleSites);
  $('catalog-notice').hidden = stats.complete || !stats.eligibleSites;
  $('catalog-notice').textContent = 'Collection is still in progress. Counts show checked links, not every matching repository.';
  $('updated-at').textContent = `Catalog updated ${new Date(stats.generatedAt).toLocaleDateString()}. Newest repository updates appear first.`;
  const search = new URL(stats.searchUrl);
  if (search.protocol === 'https:' && search.hostname === 'github.com' && search.pathname === '/search') $('search-link').href = search.href;
}
async function loadPage(reset = false) {
  if (!state.adapter) return;
  const request = ++state.request;
  const offset = reset ? 0 : state.nextOffset;
  if (offset === null) return;
  state.loading = true;
  if (reset) {
    state.shown = 0; state.total = 0; state.seen.clear(); state.generation = null;
    $('site-grid').replaceChildren(); $('load-more').hidden = true; $('directory-count').textContent = '';
    directoryMessage('Loading games…', 'Getting your selection ready.');
  }
  $('load-status').textContent = ''; updateControls();
  try {
    const result = await state.adapter.loadSites({ query: state.query, offset, limit: 12 });
    if (request !== state.request) return;
    if (state.generation && result.generation !== state.generation) { await loadPage(true); return; }
    state.generation = result.generation; state.total = result.total; state.nextOffset = result.nextOffset;
    for (const site of result.sites) {
      if (!validSite(site) || state.seen.has(site.id)) continue;
      state.seen.add(site.id); state.shown++; $('site-grid').append(siteCard(site));
    }
    $('directory-state').hidden = state.shown > 0;
    if (!state.shown) directoryMessage('No games found.', state.query ? 'Try a shorter search or clear it.' : 'The collector has not published any matching links yet.');
    $('directory-count').textContent = state.shown ? `Showing ${count.format(state.shown)} of ${count.format(state.total)}` : '';
    $('load-more').hidden = state.nextOffset === null;
  } catch {
    if (request !== state.request) return;
    if (reset) directoryMessage('The collection could not load.', 'Try again. Your play history is still available.', true);
    else $('load-status').textContent = 'Could not load more games. Try again.';
  } finally { if (request === state.request) { state.loading = false; updateControls(); } }
}
function openDialog(id) { if (!$(id).open) $(id).showModal(); }
function closeDialog(id) { if ($(id).open) $(id).close(); }
function renderHistory() {
  const list = $('player-history-list'); list.replaceChildren();
  trail.entries.map((site, index) => ({site, index})).reverse().forEach(({site, index}) => {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'list-group-item list-group-item-action';
    button.setAttribute('aria-current', String(index === trail.cursor));
    const name = document.createElement('strong'); name.textContent = site.name;
    const repo = document.createElement('small'); repo.textContent = site.repo;
    button.append(name, repo); button.addEventListener('click', () => navigateHistory(index)); list.append(button);
  });
  $('player-history-empty').hidden = trail.entries.length > 0;
}
function openPlayer(site) {
  state.site = site;
  closeDialog('player-history-drawer'); closeDialog('player-more-dialog');
  $('player-name').textContent = site.name; $('player-repo').textContent = site.repo;
  $('player-direct').href = site.url; $('player-star').href = sourceURL(site); $('player-report').href = reportURL(site);
  $('player-loading').textContent = 'Loading game…'; $('player-loading').hidden = false;
  // Replace the browsing context, not just src: previous games cannot add
  // navigations to this player's trail, keep audio running or retain focus.
  const frame = document.createElement('iframe'); frame.id = 'player-frame'; frame.title = `Game: ${site.name}`;
  frame.referrerPolicy = 'no-referrer'; frame.allow = 'autoplay; fullscreen; gamepad'; frame.allowFullscreen = true;
  const crossOrigin = new URL(site.url).origin !== location.origin;
  frame.setAttribute('sandbox', `allow-scripts allow-forms allow-pointer-lock${crossOrigin ? ' allow-same-origin' : ''}`);
  frame.addEventListener('load', () => {
    if ($('player-frame') === frame) $('player-loading').hidden = true;
    // load cannot prove a cross-origin game works; More always offers direct-open.
  });
  frame.src = site.url; $('solo-frame-host').replaceChildren(frame);
  document.body.classList.add('player-active'); openDialog('game-player');
  updateControls(); $('player-next').focus({preventScroll: true});
}
function locationFor(site) {
  const url = new URL(location.href); url.searchParams.delete('play');
  if (site) url.searchParams.set('play', site.id);
  return url.pathname + url.search + url.hash;
}
function navigateToGame(site) {
  if (!validSite(site) || !trail.visit(site)) return;
  state.selectionRequest++; state.busy = false;
  history.pushState({uselessVibe: 1, id: site.id, cursor: trail.cursor}, '', locationFor(site)); openPlayer(site);
}
function navigateHistory(index) {
  const site = trail.goto(index); if (!site) return;
  state.selectionRequest++; state.busy = false;
  history.pushState({uselessVibe: 1, id: site.id, cursor: trail.cursor}, '', locationFor(site)); openPlayer(site);
}
function showHome() {
  state.selectionRequest++; state.busy = false; state.site = null;
  closeDialog('player-history-drawer'); closeDialog('player-more-dialog'); closeDialog('game-player');
  $('solo-frame-host').replaceChildren(); document.body.classList.remove('player-active'); updateControls();
}
function closePlayer() { history.pushState({uselessVibe: 1, id: null}, '', locationFor(null)); showHome(); }
function restoreNavigation(event) {
  const stored = event?.state;
  const id = stored?.uselessVibe === 1 ? stored.id : new URL(location.href).searchParams.get('play');
  let site = null;
  if (typeof id === 'string') {
    const entry = trail.entries[stored?.cursor];
    site = entry?.id === id ? trail.goto(stored.cursor) : trail.byId(id);
  }
  state.selectionRequest++; state.busy = false;
  if (site && validSite(site)) openPlayer(site); else showHome();
}
async function takeDetour() {
  if (!state.adapter || state.busy || state.loading || !state.total) return;
  state.busy = true; const request = ++state.selectionRequest; updateControls();
  $('random-status').textContent = '';
  try {
    let result;
    for (let attempt = 0; attempt < 6; attempt++) {
      result = await state.adapter.randomSite({query: state.query});
      if (request !== state.selectionRequest) return;
      if (!trail.recentIds.has(result.site.id) || state.total === 1) break;
    }
    if (!validSite(result?.site)) throw new Error('Invalid game');
    navigateToGame(result.site);
  } catch {
    if (request !== state.selectionRequest) return;
    const message = 'Could not choose a game. Try again or change your search.';
    $('random-status').textContent = message;
    if (playerVisible()) { $('player-loading').textContent = message; $('player-loading').hidden = false; }
  } finally { if (request === state.selectionRequest) state.busy = false; updateControls(); }
}
function applySearch() {
  clearTimeout(searchTimer); state.query = $('catalog-query').value.trim();
  state.selectionRequest++; state.busy = false; void loadPage(true);
}
$('random-button').addEventListener('click', takeDetour);
$('resume-button').addEventListener('click', () => navigateHistory(trail.cursor));
$('player-next').addEventListener('click', () => {
  if (trail.canForward) navigateHistory(trail.cursor + 1); else void takeDetour();
});
$('player-previous').addEventListener('click', () => { if (trail.canPrevious) navigateHistory(trail.cursor - 1); });
$('player-history').addEventListener('click', () => {
  renderHistory(); $('player-history').setAttribute('aria-expanded', 'true'); openDialog('player-history-drawer');
});
$('player-history-close').addEventListener('click', () => closeDialog('player-history-drawer'));
$('player-history-drawer').addEventListener('close', () => $('player-history').setAttribute('aria-expanded', 'false'));
$('player-more').addEventListener('click', () => openDialog('player-more-dialog'));
$('player-more-close').addEventListener('click', () => closeDialog('player-more-dialog'));
$('player-close').addEventListener('click', closePlayer);
$('game-player').addEventListener('cancel', event => { event.preventDefault(); closePlayer(); });
window.addEventListener('popstate', restoreNavigation);
$('filter-form').addEventListener('submit', event => { event.preventDefault(); applySearch(); });
$('catalog-query').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(applySearch, 220); });
$('clear-filters').addEventListener('click', () => { $('catalog-query').value = ''; applySearch(); });
$('load-more').addEventListener('click', () => void loadPage());
$('retry-button').addEventListener('click', () => void initialize());
async function initialize() {
  state.loading = true; updateControls();
  try {
    state.adapter = await import('./catalog.js');
    const [stats] = await Promise.all([state.adapter.loadStats(), loadPage(true)]);
    renderStats(stats);
  } catch { directoryMessage('The collection could not load.', 'Try again when a catalog is available.', true); }
  finally { state.loading = false; updateControls(); }
}
updateControls();
restoreNavigation({state: history.state});
void initialize();
