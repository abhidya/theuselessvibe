const $ = (id) => document.getElementById(id);
const DEFAULT_TERMS = ["play online", "play in browser", "play in your browser", "start game", "new game", "arrow keys", "WASD"];
const PAGE_SIZE = 12;
const PROJECT_REPOSITORY = "https://github.com/abhidya/theuselessvibe";
const countFormat = new Intl.NumberFormat();
const shortDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });
const state = {
  adapter: null,
  stats: null,
  agent: "all",
  terms: new Set(),
  query: "",
  source: "any",
  revision: 0,
  request: 0,
  nextOffset: null,
  total: 0,
  shown: 0,
  seen: new Set(),
  generation: null,
  loading: false,
  randomBusy: false,
  activeSite: null,
  playerRequest: 0,
  playerReturnFocus: null,
  trail: null,
  historyOpen: false,
};
let searchTimer;

function text(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function displayCount(value) {
  return nonnegativeInteger(value) ? countFormat.format(value) : "—";
}

function dateLabel(value) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? shortDate.format(time) : "Date unavailable";
}

function checkedURL(value, kind) {
  if (typeof value !== "string" || value.length > 4096) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    const pagesHost = url.hostname.endsWith(".github.io");
    if (kind === "site" && !pagesHost) return null;
    if (kind === "repo" && url.hostname !== "github.com") return null;
    if (kind === "search" && (url.hostname !== "github.com" || url.pathname !== "/search")) return null;
    if (kind === "evidence" && !pagesHost && !["github.com", "raw.githubusercontent.com"].includes(url.hostname)) return null;
    return url.href;
  } catch {
    return null;
  }
}

function validSite(site) {
  return site && typeof site === "object" && checkedURL(site.url, "site") && canonicalRepositoryURL(site) && typeof site.id === "string";
}

function canonicalRepositoryURL(site) {
  const supplied = checkedURL(site?.repoUrl, "repo");
  if (!supplied || typeof site.repo !== "string" || !/^[a-z0-9](?:[a-z0-9-]{0,38})?\/[a-z0-9._-]{1,100}$/i.test(site.repo)) return null;
  const source = new URL(supplied);
  if (source.pathname.replace(/\/$/, "").toLowerCase() !== `/${site.repo}`.toLowerCase()) return null;
  return `https://github.com/${site.repo}`;
}

function reportURL(site) {
  const siteURL = checkedURL(site.url, "site");
  const repositoryURL = canonicalRepositoryURL(site);
  if (!siteURL || !repositoryURL) return null;
  const url = new URL(`${PROJECT_REPOSITORY}/issues/new`);
  url.search = new URLSearchParams({
    template: "report-site.yml",
    title: `[Site report] ${site.repo}`,
    "site-url": siteURL,
    "source-repository": repositoryURL,
  }).toString();
  return url.href;
}

function filters() {
  return { agent: state.agent, terms: [...state.terms], query: state.query, source: state.source };
}

function hasFilters() {
  return state.agent !== "all" || state.terms.size > 0 || state.query.length > 0 || state.source !== "any";
}

function externalLink(label, url, className) {
  const link = document.createElement("a");
  link.textContent = label;
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (className) link.className = className;
  return link;
}

function evidenceLinks(site, className) {
  const container = document.createElement("div");
  container.className = className;
  const seen = new Set();
  if (Array.isArray(site.agentEvidence)) {
    for (const evidence of site.agentEvidence) {
      if (!evidence || typeof evidence !== "object") continue;
      const url = checkedURL(evidence.url, "evidence");
      if (!url || seen.has(url)) continue;
      const agent = text(evidence.agent).toLowerCase();
      const agentName = agent === "codex" ? "Codex" : agent === "claude" ? "Claude" : "Agent";
      const label = text(evidence.label, `${agentName} evidence`);
      const link = externalLink(`${label} ↗`, url, "evidence-link");
      link.title = `${agentName}: ${text(evidence.kind, "public evidence")}`;
      container.append(link);
      seen.add(url);
    }
  }
  return container;
}

function siteCard(site, index) {
  const card = document.createElement("article");
  card.className = "site-card";
  const top = document.createElement("div");
  top.className = "card-top";
  const number = document.createElement("span");
  number.className = "card-number";
  number.textContent = String(index + 1).padStart(3, "0");
  const stars = document.createElement("span");
  stars.className = "card-stars";
  stars.textContent = `☆ ${displayCount(site.stars)}`;
  stars.title = `${displayCount(site.stars)} GitHub stars at the last check`;
  top.append(number, stars);

  const heading = document.createElement("h3");
  const name = text(site.name, site.repo.split("/").pop() || "Untitled site");
  const visit = externalLink(name, checkedURL(site.url, "site"), "card-site-link");
  visit.addEventListener("click", (event) => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      event.preventDefault();
      navigateToGame(site);
    }
  });
  visit.setAttribute("aria-label", `${name} — open website in a new tab`);
  heading.append(visit);
  const repo = externalLink(site.repo, canonicalRepositoryURL(site), "card-repo");
  repo.setAttribute("aria-label", `Source repository: ${site.repo}, opens in a new tab`);
  const description = document.createElement("p");
  description.className = "card-description";
  description.textContent = text(site.description) || "A little corner of the web, waiting to be explored.";

  const signals = document.createElement("div");
  signals.className = "card-signals";
  const signalTerms = new Set();
  if (Array.isArray(site.signals)) {
    for (const signal of site.signals) {
      const term = text(signal?.term);
      if (!term || signalTerms.has(term.toLowerCase())) continue;
      const tag = document.createElement("span");
      tag.className = "signal-tag";
      tag.textContent = term;
      tag.title = signal.source === "page" ? "Phrase detected on the website" : "Phrase detected in the repository README";
      signals.append(tag);
      signalTerms.add(term.toLowerCase());
      if (signalTerms.size >= 4) break;
    }
  }
  const evidence = evidenceLinks(site, "card-evidence");
  const footer = document.createElement("div");
  footer.className = "card-footer";
  const updated = document.createElement("time");
  if (Number.isFinite(Date.parse(site.updatedAt))) updated.dateTime = site.updatedAt;
  updated.textContent = `Updated ${dateLabel(site.updatedAt)}`;
  updated.title = `Website last checked ${dateLabel(site.checkedAt)}`;
  const license = document.createElement("span");
  license.className = "card-license";
  license.textContent = "MIT";
  license.title = "Repository’s declared license";
  footer.append(updated, license);
  const actions = document.createElement("div");
  actions.className = "card-actions";
  const star = externalLink("☆ Star on GitHub", canonicalRepositoryURL(site), "card-star-link");
  star.title = "Open the source repository to give it a star on GitHub";
  star.setAttribute("aria-label", `Star ${site.repo} on GitHub, opens the source repository in a new tab`);
  const report = externalLink("Report / request removal", reportURL(site), "card-report-link");
  report.title = "Open a prefilled GitHub issue to report this site or request its removal";
  report.setAttribute("aria-label", `Report ${site.repo} or request removal, opens a GitHub issue form in a new tab`);
  actions.append(star, report);
  card.append(top, heading, repo, description);
  if (signals.childElementCount) card.append(signals);
  if (evidence.childElementCount) card.append(evidence);
  if (["pending", "unavailable"].includes(site.attributionStatus)) {
    const attribution = document.createElement("p");
    attribution.className = "card-attribution";
    attribution.textContent = site.attributionStatus === "pending" ? "Agent activity check pending" : "Agent activity check unavailable";
    attribution.title = text(site.attributionScope, "Only a limited recent project history is checked");
    card.append(attribution);
  }
  card.append(footer, actions);
  return card;
}

function showDirectoryState(title, description, retry = false) {
  $("directory-state-title").textContent = title;
  $("directory-state-description").textContent = description;
  $("directory-state").hidden = false;
  $("retry-button").hidden = !retry;
}

function setBusy() {
  const available = Boolean(state.adapter) && state.total > 0 && !state.loading;
  $("random-button").disabled = !available || state.randomBusy;
  $("random-button").classList.toggle("is-loading", state.randomBusy);
  $("random-button").setAttribute("aria-busy", String(state.randomBusy));
  if (state.randomBusy) {
    $("random-button-label").textContent = "Finding your next detour…";
  } else if (state.loading) {
    $("random-button-label").textContent = "Loading your discoveries…";
  } else if (available) {
    $("random-button-label").textContent = hasFilters() ? `Pick from ${displayCount(state.total)} matching ${state.total === 1 ? "site" : "sites"}` : "Let chance take the wheel";
  } else {
    $("random-button-label").textContent = hasFilters() ? "Try a different set of filters" : "Waiting for little discoveries";
  }
  $("random-note").textContent = hasFilters() ? "Your filters shape the next detour. Play it here and hit Next." : "One random game, right here. Keep pressing Next.";
  $("load-more").disabled = state.loading;
  $("load-more").textContent = state.loading ? "Looking a little further…" : "More little discoveries ↓";
  $("clear-filters").hidden = !hasFilters();
  $("site-grid").setAttribute("aria-busy", String(state.loading));
}

function renderTerms(terms = DEFAULT_TERMS) {
  const clean = [...new Set(terms.filter((term) => typeof term === "string" && term.trim()).map((term) => term.trim()))];
  if (!clean.length) return;
  const fragment = document.createDocumentFragment();
  for (const term of clean) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "term-chip";
    button.textContent = term;
    button.classList.toggle("is-active", state.terms.has(term));
    button.setAttribute("aria-pressed", String(state.terms.has(term)));
    button.addEventListener("click", () => {
      if (state.terms.has(term)) state.terms.delete(term);
      else state.terms.add(term);
      button.classList.toggle("is-active", state.terms.has(term));
      button.setAttribute("aria-pressed", String(state.terms.has(term)));
      applyFilters();
    });
    fragment.append(button);
  }
  $("term-filters").replaceChildren(fragment);
}

function renderStats(stats) {
  state.stats = stats;
  $("eligible-count").textContent = displayCount(stats.eligibleSites);
  $("scanned-count").textContent = displayCount(stats.scannedRepositories);
  $("matched-count").textContent = displayCount(stats.matchedRepositories);
  $("matched-count").title = "Repository search matches, which can include unavailable or unrelated websites";
  $("connection-indicator").classList.add("is-connected");
  $("connection-label").textContent = stats.complete ? "A fresh collection of small discoveries" : "More little discoveries to find";
  for (const agent of ["codex", "claude"]) {
    const count = stats.agentCounts?.[agent];
    $(`${agent}-count`).textContent = nonnegativeInteger(count) ? displayCount(count) : "";
    $(`${agent}-count`).title = "Sites with public evidence in the full catalog";
  }
  const url = checkedURL(stats.searchUrl, "search");
  if (url) $("search-link").href = url;
  if (Array.isArray(stats.terms) && stats.terms.length) renderTerms(stats.terms);
  $("updated-at").textContent = stats.generatedAt ? `Catalog refreshed ${dateLabel(stats.generatedAt)}. Newest repository updates appear first.` : "New discoveries are ordered by repository update date.";
  if (!stats.complete && stats.eligibleSites > 0) {
    $("catalog-notice").textContent = stats.carriedForwardSites > 0
      ? `A refresh is in progress. ${displayCount(stats.carriedForwardSites)} previously checked sites remain available while we look for updates.`
      : "This snapshot covers part of the matching repositories. Counts reflect the sites checked so far.";
    $("catalog-notice").hidden = false;
  } else {
    $("catalog-notice").hidden = true;
  }
}

async function loadPage({ reset = false } = {}) {
  if (!state.adapter) return;
  const request = ++state.request;
  const selection = filters();
  const offset = reset ? 0 : state.nextOffset;
  if (!reset && offset === null) return;
  state.loading = true;
  $("load-status").textContent = "";
  if (reset) {
    state.nextOffset = null;
    state.total = 0;
    state.shown = 0;
    state.seen.clear();
    state.generation = null;
    $("site-grid").replaceChildren();
    $("directory-count").textContent = "";
    $("load-more").hidden = true;
    showDirectoryState(hasFilters() ? "Looking a little closer." : "Gathering the good stuff.", "Finding the little corners that fit your curiosity.");
  }
  setBusy();
  try {
    const response = await state.adapter.loadSites({ ...selection, offset, limit: PAGE_SIZE });
    if (request !== state.request) return;
    if (!response || !Array.isArray(response.sites) || !nonnegativeInteger(response.total)) throw new Error("Invalid catalog response");
    if (state.generation && response.generation && state.generation !== response.generation) {
      state.loading = false;
      await loadPage({ reset: true });
      return;
    }
    state.generation = response.generation || state.generation;
    const fragment = document.createDocumentFragment();
    for (const site of response.sites) {
      if (!validSite(site) || state.seen.has(site.id)) continue;
      fragment.append(siteCard(site, state.shown));
      state.seen.add(site.id);
      state.shown++;
    }
    $("site-grid").append(fragment);
    state.total = response.total;
    state.nextOffset = nonnegativeInteger(response.nextOffset) && response.nextOffset > offset ? response.nextOffset : null;
    $("load-more").hidden = state.nextOffset === null;
    $("directory-state").hidden = state.shown > 0;
    if (!state.shown) {
      showDirectoryState(
        hasFilters() ? "Nothing on this little path. Yet." : "The first discoveries are on their way.",
        hasFilters() ? "Try another phrase, a shorter search, or clear the filters to explore everything." : "There are no verified websites in this snapshot yet. Check back when the next collection is ready.",
      );
      $("directory-count").textContent = "";
    } else {
      $("directory-count").textContent = `Showing ${displayCount(state.shown)} of ${displayCount(state.total)} ${hasFilters() ? "matching " : ""}${state.total === 1 ? "site" : "sites"}`;
    }
  } catch (error) {
    if (request !== state.request) return;
    if (reset) {
      showDirectoryState("A small detour in the detours.", "The directory couldn’t load. Please try again in a moment.", true);
    } else {
      $("load-status").textContent = "The next discoveries couldn’t load. Try the button again.";
      $("load-more").hidden = false;
    }
  } finally {
    if (request === state.request) {
      state.loading = false;
      setBusy();
    }
  }
}

function applyFilters() {
  clearTimeout(searchTimer);
  state.query = $("catalog-query").value.trim();
  state.revision++;
  $("selection").hidden = true;
  $("random-status").textContent = "";
  setBusy();
  void loadPage({ reset: true });
}

function showSelection(site) {
  $("selection-name").textContent = text(site.name, site.repo.split("/").pop());
  $("selection-repo").textContent = site.repo;
  $("selection-description").textContent = text(site.description) || "A little corner of the web, waiting to be explored.";
  $("selection-link").href = checkedURL(site.url, "site");
  $("selection-source").href = canonicalRepositoryURL(site);
  $("selection-source").setAttribute("aria-label", `Star ${site.repo} on GitHub, opens the source repository in a new tab`);
  $("selection-report").href = reportURL(site);
  $("selection-report").setAttribute("aria-label", `Report ${site.repo} or request removal, opens a GitHub issue form in a new tab`);
  const evidence = evidenceLinks(site, "selection-evidence");
  $("selection-evidence").replaceChildren(...evidence.childNodes);
  $("selection").hidden = false;
}

function playerVisible() {
  return !$("game-player").hidden;
}

function updateHistoryControls() {
  const trail = state.trail;
  $("player-previous").disabled = !trail?.canPrevious;
  $("player-history").disabled = !trail?.entries.length;
  $("player-history").setAttribute("aria-expanded", String(state.historyOpen));
  $("player-history-count").textContent = String(trail?.entries.length || 0);
}
function renderHistory() {
  const list = $("player-history-list");
  list.replaceChildren();
  const entries = state.trail?.entries || [];
  entries.forEach((site, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "player-history-item";
    button.classList.toggle("is-current", index === state.trail.cursor);
    button.setAttribute("aria-current", index === state.trail.cursor ? "true" : "false");
    const name = document.createElement("strong");
    name.textContent = site.name;
    const source = document.createElement("small");
    source.textContent = site.repo;
    button.append(name, source);
    button.addEventListener("click", () => navigateHistory(index));
    list.append(button);
  });
  $("player-history-empty").hidden = entries.length > 0;
  updateHistoryControls();
}
function setHistoryDrawer(open) {
  state.historyOpen = Boolean(open);
  $("player-history-drawer").hidden = !state.historyOpen;
  if (state.historyOpen) {
    renderHistory();
    $("player-history-close").focus({preventScroll:true});
  } else {
    $("player-history").focus({preventScroll:true});
  }
  updateHistoryControls();
}
function openPlayer(site) {
  if (!validSite(site)) return;
  if (!playerVisible()) state.playerReturnFocus = document.activeElement;
  state.activeSite = site;
  $("player-name").textContent = text(site.name, site.repo);
  $("player-repo").textContent = site.repo;
  $("player-direct").href = checkedURL(site.url, "site");
  $("player-star").href = canonicalRepositoryURL(site);
  $("player-report").href = reportURL(site);
  $("player-frame").title = `Game: ${text(site.name, site.repo)}`;
  $("game-player").hidden = false;
  $("game-player").setAttribute("aria-hidden", "false");
  document.body.classList.add("player-active");
  $("player-loading").hidden = false;
  $("player-loading").textContent = "Loading your next detour…";
  $("player-frame").src = checkedURL(site.url, "site");
  $("player-next").focus({ preventScroll: true });
  renderHistory();
}
function showHome() {
  state.playerRequest++;
  state.activeSite = null;
  state.historyOpen = false;
  $("player-history-drawer").hidden = true;
  $("game-player").hidden = true;
  $("game-player").setAttribute("aria-hidden", "true");
  document.body.classList.remove("player-active");
  $("player-frame").removeAttribute("src");
  $("player-loading").hidden = true;
  const focus = state.playerReturnFocus;
  if (focus && focus.isConnected) focus.focus({ preventScroll: true });
  else $("random-button").focus({ preventScroll: true });
  state.playerReturnFocus = null;
}
function historyUrl(site) {
  const url = new URL(window.location.href);
  url.searchParams.delete("play");
  if (site) url.searchParams.set("play", site.id);
  return url.pathname + url.search + url.hash;
}
function navigateToGame(site) {
  if (!validSite(site)) return;
  state.playerRequest++;
  state.trail.visit(site);
  history.pushState({uselessVibe:1, id:site.id}, "", historyUrl(site));
  state.historyOpen = false;
  $("player-history-drawer").hidden = true;
  showSelection(site);
  openPlayer(site);
}
function navigateHistory(index) {
  const entry = state.trail.goto(index);
  if (!entry) return;
  state.playerRequest++;
  history.pushState({uselessVibe:1,id:entry.id}, "", historyUrl(entry));
  state.historyOpen = false;
  $("player-history-drawer").hidden = true;
  showSelection(entry);
  openPlayer(entry);
}
function closePlayer() {
  history.pushState({uselessVibe:1, id:null}, "", historyUrl(null));
  showHome();
}
function restoreNavigation(event) {
  const id = event?.state?.uselessVibe === 1 ? event.state.id
    : new URL(window.location.href).searchParams.get("play");
  if (typeof id === "string" && state.trail?.byId(id)) {
    state.playerRequest++;
    const site = state.trail.current;
    state.historyOpen = false;
    $("player-history-drawer").hidden = true;
    showSelection(site);
    openPlayer(site);
  } else {
    showHome();
  }
}

async function takeDetour() {
  if (!state.adapter || state.randomBusy || state.loading || state.total === 0) return;
  state.randomBusy = true;
  const request = ++state.playerRequest;
  setBusy();
  if (playerVisible()) {
    $("player-next").disabled = true;
    $("player-loading").hidden = false;
    $("player-loading").textContent = "Finding another game…";
  } else {
    $("random-status").textContent = "Choosing a little corner of the internet…";
  }
  const revision = state.revision;
  try {
    let response;
    const recent = state.trail?.recentIds || new Set();
    // Avoid recent repeats when the matching catalog contains alternatives.
    for (let attempt = 0; attempt < 6; attempt++) {
      response = await state.adapter.randomSite(filters());
      if (!recent.has(response?.site?.id) || state.total <= recent.size) break;
    }
    if (request !== state.playerRequest || revision !== state.revision) return;
    const site = response?.site;
    if (!validSite(site)) throw new Error("No valid game available");
    showSelection(site);
    navigateToGame(site);
    $("random-status").textContent = "";
  } catch {
    if (request !== state.playerRequest) return;
    $("random-status").textContent = "No game could be loaded from this selection. Try different filters.";
    if (playerVisible()) {
      $("player-loading").textContent = "Could not select a new game. Try Next again, or open the current game directly.";
      $("player-loading").hidden = false;
    }
  } finally {
    state.randomBusy = false;
    $("player-next").disabled = false;
    setBusy();
  }
}

function bindEvents() {
  $("random-button").addEventListener("click", takeDetour);
  $("player-next").addEventListener("click", () => {
    if (state.trail?.canForward) navigateHistory(state.trail.cursor + 1);
    else void takeDetour();
  });
  $("player-previous").addEventListener("click", () => { if (state.trail?.canPrevious) navigateHistory(state.trail.cursor - 1); });
  $("player-history").addEventListener("click", () => setHistoryDrawer(!state.historyOpen));
  $("player-history-close").addEventListener("click", () => setHistoryDrawer(false));
  window.addEventListener("popstate", restoreNavigation);
  $("player-close").addEventListener("click", closePlayer);
  $("player-frame").addEventListener("load", () => { if (playerVisible()) $("player-loading").hidden = true; });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.historyOpen) { event.preventDefault(); setHistoryDrawer(false); }
    else if (event.key === "Escape" && playerVisible()) closePlayer();
    if (event.key === "ArrowRight" && event.altKey && playerVisible()) { event.preventDefault(); if (state.trail?.canForward) navigateHistory(state.trail.cursor + 1); else void takeDetour(); }
    if (event.key === "ArrowLeft" && event.altKey && playerVisible() && state.trail?.canPrevious) { event.preventDefault(); navigateHistory(state.trail.cursor - 1); }
  });
  $("load-more").addEventListener("click", () => void loadPage());
  $("retry-button").addEventListener("click", () => state.adapter ? void loadPage({ reset: true }) : void initialize());
  $("filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
  });
  $("match-source").addEventListener("change", () => {
    state.source = $("match-source").value;
    applyFilters();
  });
  $("catalog-query").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, 220);
  });
  document.querySelectorAll("[data-agent]").forEach((button) => {
    button.addEventListener("click", () => {
      state.agent = button.dataset.agent;
      document.querySelectorAll("[data-agent]").forEach((chip) => {
        const selected = chip.dataset.agent === state.agent;
        chip.classList.toggle("is-active", selected);
        chip.setAttribute("aria-pressed", String(selected));
      });
      applyFilters();
    });
  });
  $("clear-filters").addEventListener("click", () => {
    state.agent = "all";
    state.terms.clear();
    state.source = "any";
    $("match-source").value = "any";
    $("catalog-query").value = "";
    document.querySelectorAll("[data-agent]").forEach((button) => {
      const selected = button.dataset.agent === "all";
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    document.querySelectorAll(".term-chip").forEach((button) => {
      button.classList.remove("is-active");
      button.setAttribute("aria-pressed", "false");
    });
    applyFilters();
  });
}

async function initialize() {
  state.loading = true;
  $("retry-button").hidden = true;
  setBusy();
  try {
    state.adapter = await import("./catalog.js");
    if (!["loadStats", "loadSites", "randomSite"].every((name) => typeof state.adapter[name] === "function")) throw new Error("Catalog adapter unavailable");
    const [statsResult, sitesResult] = await Promise.allSettled([
      state.adapter.loadStats(),
      loadPage({ reset: true }),
    ]);
    if (statsResult.status === "fulfilled" && statsResult.value && typeof statsResult.value === "object") {
      renderStats(statsResult.value);
    } else {
      $("connection-label").textContent = "Catalog summary unavailable";
      $("updated-at").textContent = "Catalog freshness is temporarily unavailable.";
    }
    if (sitesResult.status === "rejected") {
      showDirectoryState("A small detour in the detours.", "The directory couldn’t load. Please try again in a moment.", true);
    }
  } catch {
    state.adapter = null;
    $("connection-label").textContent = "The catalog is getting ready";
    $("updated-at").textContent = "No catalog snapshot is available yet.";
    showDirectoryState("A doorway, waiting for discoveries.", "The catalog hasn’t been published yet. Once its first snapshot is ready, the random button and directory will come to life.", true);
  } finally {
    state.loading = false;
    setBusy();
  }
}

async function startApp() {
  const {createTrail} = await import("./trail.js");
  state.trail = createTrail(window.localStorage);
  bindEvents();
  await initialize();
  // Browser refresh and Back/Forward restore the most recent selected game.
  restoreNavigation({state:history.state});
}
renderTerms();
void startApp();
