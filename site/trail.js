/** One bounded, local-only browsing trail. The collector and public catalog are unchanged. */
export const STORAGE_KEY = "useless-vibe:trail:v1";
export const MAX_TRAIL = 100;

export function safeEntry(site) {
  if (!site || typeof site !== "object" || typeof site.id !== "string" ||
      !/^[A-Za-z0-9_-]{1,100}$/.test(site.id) ||
      typeof site.repo !== "string" ||
      !/^[a-z0-9-]+\/[a-z0-9._-]+$/i.test(site.repo) ||
      typeof site.url !== "string" || typeof site.repoUrl !== "string") return null;
  try {
    const url = new URL(site.url);
    const source = new URL(site.repoUrl);
    const owner = site.repo.split("/")[0].toLowerCase();
    if (url.protocol !== "https:" || url.host.toLowerCase() !== owner + ".github.io" ||
        url.username || url.password || url.port ||
        source.protocol !== "https:" || source.host !== "github.com" ||
        source.pathname.replace(/\/$/, "").toLowerCase() !== "/" + site.repo.toLowerCase()) return null;
  } catch { return null; }
  return {
    id: site.id, repo: site.repo, repoUrl: site.repoUrl, url: site.url,
    name: String(site.name || site.repo).slice(0, 160),
    description: String(site.description || "").slice(0, 400)
  };
}

export function createTrail(storage = null) {
  let entries = [], cursor = -1;
  try {
    const data = JSON.parse(storage?.getItem(STORAGE_KEY) || "null");
    if (data?.version === 1 && Array.isArray(data.entries) &&
        data.entries.length <= MAX_TRAIL && Number.isInteger(data.cursor)) {
      const validated = data.entries.map(safeEntry);
      if (validated.every(Boolean) && data.cursor >= -1 && data.cursor < validated.length) {
        entries = validated; cursor = data.cursor;
      }
    }
  } catch { /* Browsers can disable storage. In-memory history still works. */ }
  function persist() {
    try { storage?.setItem(STORAGE_KEY, JSON.stringify({version:1,entries,cursor})); } catch {}
  }
  return {
    get entries() { return entries.slice(); },
    get cursor() { return cursor; },
    get current() { return entries[cursor] ?? null; },
    get canPrevious() { return cursor > 0; },
    get canForward() { return cursor >= 0 && cursor < entries.length - 1; },
    get recentIds() { return new Set(entries.slice(-20).map(x=>x.id)); },
    visit(site) {
      const entry = safeEntry(site);
      if (!entry) return null;
      if (entries[cursor]?.id === entry.id) return entry;
      entries = entries.slice(0, cursor + 1);
      entries.push(entry);
      if (entries.length > MAX_TRAIL) entries = entries.slice(-MAX_TRAIL);
      cursor = entries.length - 1;
      persist(); return entry;
    },
    goto(index) {
      if (!Number.isInteger(index) || index < 0 || index >= entries.length) return null;
      cursor = index; persist(); return entries[index];
    },
    byId(id) {
      const index = entries.findLastIndex(item => item.id === id);
      return index >= 0 ? this.goto(index) : null;
    }
  };
}
