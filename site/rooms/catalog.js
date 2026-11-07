/** Reuse the existing catalog adapter for validated, lazily loaded game details. */
export const EDITION_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/i;
export const EDITION_CHANGED = 'This room’s catalog edition is no longer available. Join the current edition and share its new link.';

export async function loadRoomCatalog({ adapter, fetcher = fetch, baseUrl = new URL('../data/', import.meta.url), edition = null }) {
  if (edition !== null && !EDITION_PATTERN.test(edition)) throw new Error('Invalid catalog edition in the room link.');
  const stats = await adapter.loadStats();
  const generation = stats.generation;
  if (typeof generation !== 'string' || !EDITION_PATTERN.test(generation) || (edition && edition !== generation)) throw new Error(EDITION_CHANGED);
  if (!Number.isSafeInteger(stats.eligibleSites) || stats.eligibleSites < 1) throw new Error('No games are available in this catalog yet.');
  const response = await fetcher(new URL(`${generation}/index.json`, baseUrl));
  if (!response.ok) throw new Error('The room catalog could not load. Please try joining again.');
  const text = await response.text();
  if (text.length > 67_108_864) throw new Error('The room catalog is too large.');
  const values = JSON.parse(text);
  if (!Array.isArray(values) || values.length !== stats.eligibleSites) throw new Error('The room catalog is incomplete.');
  const ids = new Set();
  const entries = values.map((entry, index) => {
    if (!entry || entry.index !== index || typeof entry.id !== 'string' || !entry.id || entry.id.length > 160 || ids.has(entry.id) || typeof entry.name !== 'string' || typeof entry.repo !== 'string') {
      throw new Error('The room catalog contains invalid entries.');
    }
    ids.add(entry.id);
    return { index, id: entry.id, name: entry.name.slice(0, 200), repo: entry.repo.slice(0, 141) };
  });
  return {
    edition: generation,
    entries,
    async siteFor(entry) {
      // No personal discovery filters: everyone in a room uses the full same catalog.
      const result = await adapter.loadSites({ offset: entry.index, limit: 1 });
      if (result.generation !== generation || result.sites[0]?.id !== entry.id) throw new Error(EDITION_CHANGED);
      return result.sites[0];
    },
  };
}
