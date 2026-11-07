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
  $("random-note").textContent = hasFilters() ? "Your filters shape the next detour. Opens a new tab." : "One random website. A new tab. A happy detour.";
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

async function takeDetour() {
  if (!state.adapter || state.randomBusy || state.loading || state.total === 0) return;
  state.randomBusy = true;
  setBusy();
  $("random-status").textContent = "Choosing a little corner of the internet…";
  const revision = state.revision;
  // Opening during the click keeps the new tab inside the browser's user gesture.
  // Sever the opener before navigating it to any external website.
  let popup = null;
  try {
    popup = window.open("about:blank", "_blank");
    if (popup) {
      popup.opener = null;
      popup.document.title = "Finding your next detour…";
    }
  } catch {
    if (popup && !popup.closed) popup.close();
    popup = null;
  }
  try {
    const response = await state.adapter.randomSite(filters());
    if (revision !== state.revision) {
      if (popup && !popup.closed) popup.close();
      $("random-status").textContent = "Your filters changed. Click again for a new detour.";
      return;
    }
    const site = response?.site;
    if (!site) {
      if (popup && !popup.closed) popup.close();
      $("random-status").textContent = "No discoveries match these filters yet. Try a different phrase.";
      return;
    }
    if (!validSite(site)) throw new Error("Invalid site URL");
    showSelection(site);
    if (popup && !popup.closed) {
      popup.location.replace(checkedURL(site.url, "site"));
      $("random-status").textContent = "Your detour opened in a new tab. Enjoy the unexpected.";
    } else {
      $("random-status").textContent = "Your browser kept the new tab closed. Open your discovery below.";
      $("selection-link").focus({ preventScroll: true });
      $("selection").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    }
  } catch {
    if (popup && !popup.closed) popup.close();
    $("random-status").textContent = "That detour couldn’t load. Give it another try.";
  } finally {
    state.randomBusy = false;
    setBusy();
  }
}

function bindEvents() {
  $("random-button").addEventListener("click", takeDetour);
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

renderTerms();
bindEvents();
void initialize();
