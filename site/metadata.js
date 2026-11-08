/** Additive catalog metadata. Missing fields remain unknown; tags never imply runtime verification. */
export const TAG_FIELDS = ['genres', 'modes', 'engines', 'libraries', 'languages', 'controls', 'contentLanguages'];
export const FILTER_FIELDS = {genre: 'genres', mode: 'modes', engine: 'engines', language: 'languages', control: 'controls', contentLanguage: 'contentLanguages'};
const text = (v, max = 200) => typeof v === 'string' && v.length <= max;
const ensure = condition => { if (!condition) throw new TypeError('Invalid catalog metadata'); };
export function parseMetadata(value, repo = null) {
  if (value == null) value = {schemaVersion: 1};
  ensure(value && typeof value === 'object' && !Array.isArray(value) && value.schemaVersion === 1);
  const result = {schemaVersion: 1};
  for (const key of TAG_FIELDS) {
    const values = value[key] ?? [];
    ensure(Array.isArray(values) && values.length <= 40 && values.every(v => text(v, 64) && v.length && !/[\u0000-\u001f]/.test(v)));
    result[key] = [...new Set(values)].sort();
  }
  const players = value.playerCount ?? null;
  ensure(players === null || (Number.isInteger(players.min) && Number.isInteger(players.max) && players.min >= 1 && players.min <= players.max && players.max <= 64));
  result.playerCount = players === null ? null : {min: players.min, max: players.max};
  result.sourceStatus = value.sourceStatus ?? 'pending';
  ensure(['pending', 'checked', 'partial'].includes(result.sourceStatus));
  result.rulesVersion = value.rulesVersion ?? null;
  ensure(result.rulesVersion === null || text(result.rulesVersion, 80));
  result.sourceRevision = value.sourceRevision ?? null;
  ensure(result.sourceRevision === null || /^[a-f0-9]{40}$/.test(result.sourceRevision));
  result.observedAt = value.observedAt ?? null;
  ensure(result.observedAt === null || (text(result.observedAt, 40) && Number.isFinite(Date.parse(result.observedAt))));
  const evidence = value.evidence ?? [];
  ensure(Array.isArray(evidence) && evidence.length <= 64);
  result.evidence = evidence.map(e => {
    ensure(e && [...TAG_FIELDS, 'playerCount'].includes(e.field) && ['declared', 'detected'].includes(e.strength));
    ensure(text(e.rule, 120) && text(e.path, 512) && text(e.excerpt, 180) && text(e.url, 2048));
    const url = new URL(e.url);
    ensure(url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port);
    if (repo) ensure(url.pathname.toLowerCase() === `/${repo}`.toLowerCase() || url.pathname.toLowerCase().startsWith(`/${repo}/`.toLowerCase()));
    ensure(e.field === 'playerCount' ? e.value && Number.isInteger(e.value.min) && Number.isInteger(e.value.max) : text(e.value, 64));
    return {field: e.field, value: structuredClone(e.value), rule: e.rule, strength: e.strength,
      path: e.path, excerpt: e.excerpt, url: url.href, revision: e.revision ?? null, line: Number.isSafeInteger(e.line) ? e.line : null};
  });
  result.unknownFields = TAG_FIELDS.filter(f => !result[f].length);
  if (!result.playerCount) result.unknownFields.push('playerCount');
  result.mobileCompatibility = 'unknown'; result.iframeCompatibility = 'unknown'; result.playability = 'unverified';
  return result;
}

export function metadataFilters(options = {}) {
  const result = {};
  for (const filter of Object.keys(FILTER_FIELDS)) {
    const value = options[filter] ?? '';
    ensure(text(value, 64)); result[filter] = value.trim().toLowerCase();
  }
  const players = options.players ?? '';
  ensure(players === '' || (Number.isInteger(players) && players >= 1 && players <= 64));
  result.players = players;
  for (const field of ['createdAfter', 'updatedAfter', 'checkedAfter']) {
    const value = options[field] ?? '';
    ensure(value === '' || (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0,10) === value));
    result[field] = value;
  }
  return result;
}
export const hasMetadataFilters = filters => Object.values(filters).some(v => v !== '');

export function matchesMetadata(entry, filters) {
  const metadata = entry.metadata ?? {};
  for (const [filter, field] of Object.entries(FILTER_FIELDS)) {
    const value = filters[filter];
    if (!value) continue;
    const values = metadata[field] ?? [];
    if (value === 'unknown') { if (values.length) return false; }
    else if (filter === 'mode' && value === 'multiplayer') {
      if (!values.some(v => v.includes('multiplayer'))) return false;
    } else if (!values.some(v => v.toLowerCase() === value)) return false;
  }
  if (filters.players !== '') {
    const range = metadata.playerCount;
    if (!range || filters.players < range.min || filters.players > range.max) return false;
  }
  for (const [filter, field] of [['createdAfter', 'createdAt'], ['updatedAfter', 'updatedAt'], ['checkedAfter', 'checkedAt']]) {
    if (filters[filter] && (!entry[field] || !Number.isFinite(Date.parse(entry[field])) || Date.parse(entry[field]) < Date.parse(filters[filter]))) return false;
  }
  return true;
}

export function facetCounts(entries) {
  const counts = Object.fromEntries(TAG_FIELDS.map(key => [key, Object.create(null)]));
  for (const entry of entries) {
    for (const field of TAG_FIELDS) {
      const values = entry.metadata?.[field] ?? [];
      for (const value of values.length ? values : ['unknown']) counts[field][value] = (counts[field][value] || 0) + 1;
    }
  }
  return counts;
}
