import {isText} from './text.js';
import {parseMetadata, metadataFilters, hasMetadataFilters, matchesMetadata, facetCounts} from './metadata.js';
/**
 * Static catalog adapter. All files are public GitHub Pages assets exported
 * from SQLite. A generation is immutable; the manifest switches last.
 * Filters: agent/source intersect; any selected term matches; query words all match.
 */
const MANIFEST_TTL_MS = 30_000;
const MAX_CACHED_SHARDS = 8;
const UINT32_RANGE = 0x1_0000_0000;
// Validate existing API-returned owners, not GitHub's new-account signup rules:
// historical logins such as Kline- end in a hyphen. Share this bounded pattern
// across repository names and Pages hosts; exact owner/origin checks stay below.
const OWNER = '[a-z0-9][a-z0-9-]{0,38}';
const REPOSITORY = new RegExp(`^(${OWNER})/([a-z0-9._-]{1,100})$`, 'i');
const PAGES_HOST = new RegExp(`^${OWNER}\\.github\\.io$`, 'i');

export class CatalogError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'CatalogError';
    this.code = code;
  }
}

class MissingCatalogFileError extends CatalogError {
  constructor() {
    super('CATALOG_UNAVAILABLE', 'The catalog could not be loaded. Please try again shortly.');
  }
}

function assert(condition) {
  if (!condition) throw new CatalogError('CATALOG_UNAVAILABLE', 'The catalog could not be loaded. Please try again shortly.');
}

function isCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function isDate(value) {
  return isText(value, 40) && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
}

function safeHttpsUrl(value) {
  assert(isText(value, 4096));
  assert(value === value.trim() && !/[\u0000-\u0020\u007f\\]/.test(value));
  const authority = /^https:\/\/([^/?#]+)/i.exec(value)?.[1];
  assert(authority && !authority.includes('@') && !authority.includes(':'));
  let url;
  try { url = new URL(value); } catch { assert(false); }
  assert(url.protocol === 'https:' && !url.username && !url.password && !url.port);
  return url;
}

function parseManifest(value) {
  assert(value && typeof value === 'object' && !Array.isArray(value));
  assert(value.schemaVersion === 1 && isText(value.generation, 128));
  assert(/^[a-z0-9][a-z0-9._-]*$/i.test(value.generation));
  assert(isText(value.query, 4096, true) && isDate(value.generatedAt));
  const search = safeHttpsUrl(value.searchUrl);
  assert(search.hostname === 'github.com' && search.pathname === '/search');
  assert(value.matchedRepositories === null || isCount(value.matchedRepositories));
  assert(isCount(value.scannedRepositories) && isCount(value.eligibleSites));
  assert(value.eligibleSites <= UINT32_RANGE);
  assert(typeof value.complete === 'boolean' && value.shardSize === 256);
  assert(value.indexKey === `${value.generation}/index.json` && Array.isArray(value.shards));
  const keys = new Set();
  let total = 0;
  const shards = value.shards.map((entry) => {
    assert(entry && typeof entry === 'object' && isText(entry.key, 256));
    assert(entry.key.startsWith(`${value.generation}/`));
    const basename = entry.key.slice(value.generation.length + 1);
    assert(/^\d{4,12}\.json$/.test(basename) && !keys.has(entry.key));
    assert(Number.isInteger(entry.count) && entry.count >= 1 && entry.count <= 256);
    keys.add(entry.key);
    const shard = { key: entry.key, count: entry.count, start: total };
    total += entry.count;
    return shard;
  });
  assert(total === value.eligibleSites);
  const agentCounts = value.agentCounts ?? {};
  const termCounts = value.termCounts ?? {};
  assert(agentCounts && typeof agentCounts === 'object' && !Array.isArray(agentCounts));
  assert(termCounts && typeof termCounts === 'object' && !Array.isArray(termCounts));
  for (const count of [...Object.values(agentCounts), ...Object.values(termCounts)]) assert(isCount(count));
  const terms = value.terms ?? Object.keys(termCounts);
  assert(Array.isArray(terms) && terms.every((term) => isText(term, 200)));
  const stats = {
    schemaVersion: 1,
    generation: value.generation,
    query: value.query,
    searchUrl: search.href,
    generatedAt: value.generatedAt,
    matchedRepositories: value.matchedRepositories,
    scannedRepositories: value.scannedRepositories,
    eligibleSites: value.eligibleSites,
    complete: value.complete,
    agentCounts: { ...agentCounts },
    terms: [...terms],
    termCounts: { ...termCounts },
  };
  for (const key of ['pendingMetadataChecks', 'metadataCheckedSites', 'metadataPartialSites', 'pendingAttributionChecks', 'unavailableAttributionChecks', 'carriedForwardSites', 'currentScanEligibleSites', 'pendingPartitions', 'pendingRepositoryChecks', 'discoveredRepositories']) {
    if (value[key] !== undefined) {
      assert(isCount(value[key]));
      stats[key] = value[key];
    }
  }
  for (const key of ['searchQueries', 'unresolvedPartitions', 'coverageNote', 'reportedMatchesByQuery', 'scanStartedAt', 'scanFinishedAt', 'attributionScope']) {
    if (value[key] !== undefined) stats[key] = value[key];
  }
  return { stats, generation: value.generation, indexKey: value.indexKey, total, shards };
}

function normalizedStrings(values, maxLength = 200) {
  assert(Array.isArray(values) && values.every((value) => isText(value, maxLength)));
  return [...new Set(values.map((value) => value.toLowerCase()))];
}

function parseSite(value) {
  assert(value && typeof value === 'object' && !Array.isArray(value));
  assert(isText(value.id, 160) && isText(value.repo, 141));
  const repo = REPOSITORY.exec(value.repo);
  assert(repo && isText(value.name, 200) && isText(value.description, 4096, true));
  const page = safeHttpsUrl(value.url);
  assert(PAGES_HOST.test(page.hostname) && page.hostname === `${repo[1].toLowerCase()}.github.io`);
  const source = safeHttpsUrl(value.repoUrl);
  assert(source.hostname === 'github.com' && !source.search && !source.hash);
  assert(source.pathname.replace(/\/$/, '').toLowerCase() === `/${value.repo}`.toLowerCase());
  // Discovery admits small repositories; already-admitted games can gain stars.
  assert((value.stars == null || isCount(value.stars)) && value.license === 'MIT');
  assert(value.starsCheckedAt == null || isDate(value.starsCheckedAt));
  assert(isDate(value.updatedAt) && isDate(value.createdAt) && isDate(value.checkedAt));
  const agents = normalizedStrings(value.agents ?? [], 32);
  assert(Array.isArray(value.signals ?? []) && Array.isArray(value.agentEvidence ?? []));
  const signals = (value.signals ?? []).map((signal) => {
    assert(signal && isText(signal.term, 200) && ['readme', 'page'].includes(signal.source));
    return { term: signal.term, source: signal.source };
  });
  const agentEvidence = (value.agentEvidence ?? []).map((evidence) => {
    assert(evidence && isText(evidence.agent, 32) && isText(evidence.kind, 80));
    assert(isText(evidence.label, 500));
    const url = safeHttpsUrl(evidence.url);
    assert(url.hostname === 'github.com' || PAGES_HOST.test(url.hostname));
    return { agent: evidence.agent, kind: evidence.kind, url: url.href, label: evidence.label };
  });
  const attributionStatus = value.attributionStatus ?? 'pending';
  assert(['pending', 'checked', 'unavailable'].includes(attributionStatus));
  assert(value.attributionCheckedAt == null || isDate(value.attributionCheckedAt));
  assert(value.attributionScope == null || isText(value.attributionScope, 500));
  return {
    id: value.id,
    repo: value.repo,
    name: value.name,
    description: value.description,
    url: page.href,
    repoUrl: source.href,
    stars: value.stars ?? null,
    starsCheckedAt: value.starsCheckedAt ?? null,
    license: 'MIT',
    updatedAt: value.updatedAt,
    createdAt: value.createdAt,
    checkedAt: value.checkedAt,
    agents,
    signals,
    agentEvidence,
    attributionStatus,
    metadata: parseMetadata(value.metadata, value.repo),
    attributionCheckedAt: value.attributionCheckedAt ?? null,
    attributionScope: value.attributionScope ?? null,
  };
}

function locateShard(shards, index) {
  let low = 0;
  let high = shards.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const shard = shards[middle];
    if (index < shard.start) high = middle - 1;
    else if (index >= shard.start + shard.count) low = middle + 1;
    else return shard;
  }
  throw new CatalogError('CATALOG_UNAVAILABLE', 'The catalog could not be loaded. Please try again shortly.');
}

function popularityOrder(a, b) {
  // A missing historical count is unknown, not an observed zero. Ties retain
  // the export's deterministic order without changing detail-shard positions.
  if (a.stars === null) return b.stars === null ? a.index - b.index : 1;
  if (b.stars === null) return -1;
  return b.stars - a.stars || a.index - b.index;
}

/** Rejection sampling prevents modulo bias for every matching-set size. */
export function uniformRandomIndex(count, fillRandomValues = (values) => crypto.getRandomValues(values)) {
  if (!Number.isSafeInteger(count) || count < 1 || count > UINT32_RANGE) throw new RangeError('Invalid random selection size');
  const limit = UINT32_RANGE - (UINT32_RANGE % count);
  const values = new Uint32Array(1);
  do fillRandomValues(values); while (values[0] >= limit);
  return values[0] % count;
}

function filterOptions({ agent = 'all', terms = [], query = '', source = 'any' } = {}) {
  if (!isText(agent, 32) || !Array.isArray(terms) || !terms.every((term) => isText(term, 200)) || !isText(query, 1000, true) || !['any', 'page', 'readme'].includes(source)) {
    throw new CatalogError('INVALID_FILTER', 'Choose a valid agent, terms, and search query.');
  }
  return {
    agent: agent.toLowerCase(),
    source,
    terms: [...new Set(terms.map((term) => term.trim().toLowerCase()).filter(Boolean))],
    words: query.normalize('NFKC').trim().toLowerCase().split(/\s+/).filter(Boolean),
  };
}

export function createCatalog({
  baseUrl = new URL('./data/', import.meta.url),
  fetcher = (...args) => fetch(...args),
  now = Date.now,
  fillRandomValues,
} = {}) {
  const base = new URL(baseUrl);
  if (!base.pathname.endsWith('/')) base.pathname += '/';
  let cachedManifest = null;
  let manifestExpiresAt = 0;
  let pendingManifest = null;
  const shardCache = new Map();
  const indexCache = new Map();

  async function readJson(key, maxBytes, fresh = false) {
    const url = new URL(key, base);
    assert(url.origin === base.origin && url.pathname.startsWith(base.pathname));
    try {
      const response = await fetcher(url.href, { cache: fresh ? 'no-cache' : 'default' });
      if (key !== 'manifest.json' && [404, 410].includes(response.status)) {
        throw new MissingCatalogFileError();
      }
      assert(response.ok);
      const header = response.headers?.get('Content-Length');
      if (header) assert(Number(header) <= maxBytes);
      const text = await response.text();
      assert(text.length <= maxBytes && new TextEncoder().encode(text).byteLength <= maxBytes);
      return JSON.parse(text);
    } catch (error) {
      if (error instanceof CatalogError) throw error;
      throw new CatalogError('CATALOG_UNAVAILABLE', 'The catalog could not be loaded. Please try again shortly.');
    }
  }

  async function manifest(forceRefresh = false) {
    if (!forceRefresh && cachedManifest && now() < manifestExpiresAt) return cachedManifest;
    if (pendingManifest) return pendingManifest;
    pendingManifest = (async () => {
      const value = parseManifest(await readJson('manifest.json', 1_048_576, true));
      cachedManifest = value;
      manifestExpiresAt = now() + MANIFEST_TTL_MS;
      return value;
    })();
    try { return await pendingManifest; } finally { pendingManifest = null; }
  }

  async function withGenerationRetry(operation) {
    const value = await manifest();
    try {
      return await operation(value);
    } catch (error) {
      if (!(error instanceof MissingCatalogFileError)) throw error;
      // A Pages deployment may remove the old generation while this tab still
      // has its manifest cached. Refresh once and restart the whole operation
      // so no page mixes detail files or index positions from two generations.
      const refreshed = cachedManifest?.generation !== value.generation
        ? cachedManifest
        : await manifest(true);
      if (refreshed.generation === value.generation) throw error;
      return operation(refreshed);
    }
  }

  async function cachedRead(cache, key, load, maxEntries) {
    if (cache.has(key)) {
      const pending = cache.get(key);
      cache.delete(key);
      cache.set(key, pending);
      return pending;
    }
    const pending = load();
    cache.set(key, pending);
    while (cache.size > maxEntries) cache.delete(cache.keys().next().value);
    try { return await pending; } catch (error) {
      if (cache.get(key) === pending) cache.delete(key);
      throw error;
    }
  }

  async function shard(value, definition) {
    return cachedRead(shardCache, definition.key, async () => {
      const sites = await readJson(definition.key, 4_194_304);
      assert(Array.isArray(sites) && sites.length === definition.count);
      return sites.map(parseSite);
    }, MAX_CACHED_SHARDS);
  }

  async function searchIndex(value, order = 'index') {
    const cached = await cachedRead(indexCache, value.indexKey, async () => {
      const values = await readJson(value.indexKey, 67_108_864);
      assert(Array.isArray(values) && values.length === value.total);
      const entries = values.map((entry, index) => {
        assert(entry && entry.index === index && isText(entry.id, 160));
        assert(isText(entry.repo, 141) && REPOSITORY.test(entry.repo));
        assert(isText(entry.name, 200) && isText(entry.description, 160, true));
        assert(entry.stars == null || isCount(entry.stars));
        assert(entry.starsCheckedAt == null || isDate(entry.starsCheckedAt));
        const agents = normalizedStrings(entry.agents ?? [], 32);
        const terms = normalizedStrings(entry.terms ?? []);
        return {
          index,
          id: entry.id,
          stars: entry.stars ?? null,
          starsCheckedAt: entry.starsCheckedAt ?? null,
          metadata: parseMetadata(entry.metadata, entry.repo),
          createdAt: entry.createdAt ?? null, updatedAt: entry.updatedAt ?? null, checkedAt: entry.checkedAt ?? null,
          agents,
          terms,
          pageTerms: normalizedStrings(entry.pageTerms ?? []),
          readmeTerms: normalizedStrings(entry.readmeTerms ?? []),
          text: [entry.repo, entry.name, entry.description, ...terms].join(' ').normalize('NFKC').toLowerCase(),
        };
      });
      return { index: entries, stars: [...entries].sort(popularityOrder) };
    }, 1);
    return cached[order];
  }

  async function matches(value, options, order = 'index') {
    const filter = filterOptions(options);
    const extra = metadataFilters(options);
    if (!hasMetadataFilters(extra) && filter.agent === 'all' && filter.terms.length === 0 && filter.words.length === 0 && filter.source === 'any') return null;
    const entries = await searchIndex(value, order);
    return entries.filter((entry) => {
      const terms = filter.source === 'any' ? entry.terms : entry[`${filter.source}Terms`];
      const termMatch = filter.terms.length > 0
        ? filter.terms.some((term) => terms.includes(term))
        : filter.source === 'any' || terms.length > 0;
      return (filter.agent === 'all' || entry.agents.includes(filter.agent)) &&
        termMatch && filter.words.every((word) => entry.text.includes(word)) && matchesMetadata(entry, extra);
    });
  }

  async function getSites(value, indices, expectedIds) {
    const needed = new Map();
    for (const index of indices) {
      const definition = locateShard(value.shards, index);
      if (!needed.has(definition.key)) needed.set(definition.key, { definition, values: await shard(value, definition) });
    }
    return indices.map((index, position) => {
      const definition = locateShard(value.shards, index);
      const site = needed.get(definition.key).values[index - definition.start];
      if (expectedIds) assert(site.id === expectedIds[position]);
      // Callers cannot mutate our cached catalog through returned nested arrays.
      return structuredClone(site);
    });
  }

  return {
    async loadFacets() {
      return withGenerationRetry(async (value) => ({generation: value.generation, facets: facetCounts(await searchIndex(value))}));
    },
    async loadStats() {
      return structuredClone((await manifest()).stats);
    },
    async loadSites(options = {}) {
      const { offset = 0, limit = 12, order = 'stars' } = options;
      if (!isCount(offset) || !Number.isSafeInteger(limit) || limit < 1 || limit > 50) {
        throw new CatalogError('INVALID_PAGINATION', 'Use a non-negative offset and a limit from 1 to 50.');
      }
      if (!['stars', 'index'].includes(order)) {
        throw new CatalogError('INVALID_ORDER', 'Choose stars or index order.');
      }
      return withGenerationRetry(async (value) => {
        let matching = await matches(value, options, order);
        // Browse ranks the complete matching set before pagination. Rooms use
        // index order explicitly to look up their deterministic entry positions.
        if (matching === null && order === 'stars') matching = await searchIndex(value, order);
        const total = matching === null ? value.total : matching.length;
        const end = Math.min(offset + limit, total);
        const entries = matching === null ? null : matching.slice(offset, end);
        const indices = entries ? entries.map((entry) => entry.index) : Array.from({ length: Math.max(0, end - offset) }, (_, index) => offset + index);
        const sites = await getSites(value, indices, entries?.map((entry) => entry.id));
        return { sites, total, offset, nextOffset: end < total ? end : null, generation: value.generation };
      });
    },
    async randomSite(options = {}) {
      return withGenerationRetry(async (value) => {
        const matching = await matches(value, options);
        const count = matching === null ? value.total : matching.length;
        if (count === 0) throw new CatalogError('EMPTY_CATALOG', 'No verified games match these filters yet.');
        const selected = uniformRandomIndex(count, fillRandomValues);
        const index = matching === null ? selected : matching[selected].index;
        const [site] = await getSites(value, [index], matching === null ? undefined : [matching[selected].id]);
        return { site };
      });
    },
  };
}

const catalog = createCatalog();
export const loadStats = (...args) => catalog.loadStats(...args);
export const loadSites = (...args) => catalog.loadSites(...args);
export const randomSite = (...args) => catalog.randomSite(...args);

export const loadFacets = (...args) => catalog.loadFacets(...args);
