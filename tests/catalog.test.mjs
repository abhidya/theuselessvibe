import test from 'node:test';
import assert from 'node:assert/strict';
import { createCatalog, CatalogError, uniformRandomIndex } from '../site/catalog.js';

const DATE = '2026-11-06T00:00:00Z';
function site(index, extra = {}) {
  return {
    id: String(index), repo: `alice/game-${index}`, name: `Game ${index}`,
    description: `A browser puzzle number ${index}`, url: `https://alice.github.io/game-${index}/`,
    repoUrl: `https://github.com/alice/game-${index}`, stars: index % 2,
    license: 'MIT', updatedAt: DATE, createdAt: DATE, checkedAt: DATE,
    agents: [], signals: [], agentEvidence: [],
    attributionStatus: 'checked', attributionCheckedAt: DATE, attributionScope: 'Latest commits and pull requests',
    ...extra,
  };
}
function fixture(sites = Array.from({ length: 260 }, (_, i) => site(i)), generation = 'v1') {
  const files = new Map();
  const shards = [];
  for (let offset = 0; offset < sites.length; offset += 256) {
    const key = `${generation}/${String(shards.length).padStart(4, '0')}.json`;
    const values = sites.slice(offset, offset + 256);
    files.set(key, values);
    shards.push({ key, count: values.length });
  }
  files.set(`${generation}/index.json`, sites.map((value, index) => ({
    index, id: value.id, repo: value.repo, name: value.name, description: value.description.slice(0, 160),
    agents: value.agents, terms: [...new Set(value.signals.map((signal) => signal.term.toLowerCase()))],
    pageTerms: value.signals.filter((signal) => signal.source === 'page').map((signal) => signal.term.toLowerCase()),
    readmeTerms: value.signals.filter((signal) => signal.source === 'readme').map((signal) => signal.term.toLowerCase()),
  })));
  files.set('manifest.json', {
    schemaVersion: 1, generation, query: 'game "github.io" in:readme stars:<2 license:mit',
    searchUrl: 'https://github.com/search?q=game&type=repositories&s=updated&o=desc',
    generatedAt: DATE, matchedRepositories: 38400, scannedRepositories: 1000,
    eligibleSites: sites.length, complete: false, shardSize: 256,
    indexKey: `${generation}/index.json`, shards, agentCounts: { codex: 0, claude: 0 },
    terms: ['play online', 'arrow keys'], termCounts: {}, pendingAttributionChecks: 3,
    discoveredRepositories: 1500, reportedMatchesByQuery: [{ query: 'game', count: 38400, incomplete: false }],
  });
  const calls = [];
  const fetcher = async (url) => {
    const key = new URL(url).pathname.replace('/project/data/', '');
    calls.push(key);
    return files.has(key)
      ? new Response(JSON.stringify(files.get(key)), { status: 200 })
      : new Response('Missing', { status: 404 });
  };
  return { files, calls, options: { baseUrl: 'https://example.org/project/data/', fetcher } };
}
const rejectCatalog = (promise) => assert.rejects(promise, (error) => error instanceof CatalogError && error.code === 'CATALOG_UNAVAILABLE');

test('unfiltered pages skip the compact index and stats expose coverage without shard internals', async () => {
  const data = fixture();
  const api = createCatalog(data.options);
  const stats = await api.loadStats();
  const page = await api.loadSites();
  assert.equal(stats.matchedRepositories, 38400);
  assert.equal(stats.eligibleSites, 260);
  assert.equal(stats.pendingAttributionChecks, 3);
  assert.equal(stats.discoveredRepositories, 1500);
  assert.deepEqual(stats.reportedMatchesByQuery, [{ query: 'game', count: 38400, incomplete: false }]);
  assert.equal('shards' in stats, false);
  assert.deepEqual(page.sites.map((value) => value.id), Array.from({ length: 12 }, (_, i) => String(i)));
  assert.equal(page.nextOffset, 12);
  assert.deepEqual(data.calls, ['manifest.json', 'v1/0000.json']);
});

test('pagination spans shard boundaries and handles partial, empty and out-of-range pages', async () => {
  const data = fixture();
  const api = createCatalog(data.options);
  const page = await api.loadSites({ offset: 254, limit: 5 });
  assert.deepEqual(page.sites.map((value) => value.id), ['254', '255', '256', '257', '258']);
  assert.equal(page.nextOffset, 259);
  const last = await api.loadSites({ offset: 259, limit: 12 });
  assert.deepEqual(last.sites.map((value) => value.id), ['259']);
  assert.equal(last.nextOffset, null);
  const beyond = await api.loadSites({ offset: 10000 });
  assert.deepEqual(beyond.sites, []);
  assert.equal(beyond.total, 260);
  assert.equal(beyond.nextOffset, null);
});

test('selected terms use OR, while agent, source and query filters intersect', async () => {
  const data = fixture([
    site(0, { agents: ['codex'], signals: [{ term: 'play online', source: 'page' }] }),
    site(1, { agents: ['claude'], signals: [{ term: 'arrow keys', source: 'page' }] }),
    site(2, { agents: ['codex'], signals: [{ term: 'arrow keys', source: 'readme' }] }),
    site(3, { agents: ['codex'], signals: [{ term: 'start game', source: 'page' }] }),
  ]);
  const api = createCatalog(data.options);
  const options = { agent: 'CODEX', terms: ['play online', 'arrow keys'], query: 'browser puzzle' };
  const page = await api.loadSites(options);
  assert.deepEqual(page.sites.map((value) => value.id), ['0', '2']);
  assert.equal(page.total, 2);
  assert.deepEqual((await api.loadSites({ ...options, source: 'page' })).sites.map((value) => value.id), ['0']);
  assert.deepEqual((await api.loadSites({ terms: options.terms, source: 'page' })).sites.map((value) => value.id), ['0', '1']);
  assert.deepEqual((await api.loadSites({ source: 'readme' })).sites.map((value) => value.id), ['2']);
  assert.deepEqual((await api.loadSites({ agent: 'codex', source: 'page' })).sites.map((value) => value.id), ['0', '3']);
  assert.equal((await api.loadSites({ query: 'unrelated' })).total, 0);
  assert.equal(data.calls.filter((key) => key.endsWith('/index.json')).length, 1);
});

test('random selection gives the short final shard its correct global positions', async () => {
  const data = fixture();
  const api = createCatalog({ ...data.options, fillRandomValues: (values) => { values[0] = 259; } });
  const selected = await api.randomSite();
  assert.equal(selected.site.id, '259');
  assert.deepEqual(data.calls, ['manifest.json', 'v1/0001.json']);
});

test('filtered random selection operates on matching indices rather than all sites', async () => {
  const data = fixture([site(0), site(1, { agents: ['codex'] }), site(2), site(3, { agents: ['codex'] })]);
  const api = createCatalog({ ...data.options, fillRandomValues: (values) => { values[0] = 1; } });
  assert.equal((await api.randomSite({ agent: 'codex' })).site.id, '3');
  await assert.rejects(api.randomSite({ agent: 'claude' }), (error) => error.code === 'EMPTY_CATALOG');
});

test('random rejection sampling removes modulo bias and supports boundary sizes', () => {
  const samples = [0xffff_ffff, 19];
  let attempts = 0;
  assert.equal(uniformRandomIndex(10, (values) => { values[0] = samples[attempts++]; }), 9);
  assert.equal(attempts, 2);
  assert.equal(uniformRandomIndex(1, (values) => { values[0] = 0xffff_ffff; }), 0);
  assert.equal(uniformRandomIndex(2 ** 32, (values) => { values[0] = 0xffff_ffff; }), 0xffff_ffff);
  assert.throws(() => uniformRandomIndex(0), RangeError);
  assert.throws(() => uniformRandomIndex(2 ** 32 + 1), RangeError);
  // Every catalog position is reachable once in this complete input interval.
  const counts = Array(259).fill(0);
  for (let sample = 0; sample < 259; sample++) counts[uniformRandomIndex(259, (values) => { values[0] = sample; })]++;
  assert.ok(counts.every((count) => count === 1));
});

test('an empty catalog reports zero honestly and random rejects without fetching a shard', async () => {
  const data = fixture([]);
  const api = createCatalog(data.options);
  assert.deepEqual((await api.loadSites()).sites, []);
  assert.equal((await api.loadStats()).eligibleSites, 0);
  await assert.rejects(api.randomSite(), (error) => error.code === 'EMPTY_CATALOG');
  assert.deepEqual(data.calls, ['manifest.json']);
});

test('invalid pagination and filter choices fail explicitly', async () => {
  const data = fixture();
  const api = createCatalog(data.options);
  for (const options of [{ offset: -1 }, { offset: 1.5 }, { limit: 0 }, { limit: 51 }, { offset: '0' }]) {
    await assert.rejects(api.loadSites(options), (error) => error.code === 'INVALID_PAGINATION');
  }
  await assert.rejects(api.loadSites({ source: 'unknown' }), (error) => error.code === 'INVALID_FILTER');
});

test('missing or malformed manifests fail without fabricating results', async () => {
  const missing = fixture();
  missing.files.delete('manifest.json');
  await rejectCatalog(createCatalog(missing.options).loadStats());
  for (const change of [
    { eligibleSites: 999 }, { indexKey: 'https://attacker.example/index.json' },
    { shards: [{ key: '../private.json', count: 260 }] }, { searchUrl: 'https://%' },
  ]) {
    const data = fixture();
    Object.assign(data.files.get('manifest.json'), change);
    await rejectCatalog(createCatalog(data.options).loadStats());
  }
});

test('outgoing game URLs reject lookalikes, credentials, explicit ports, HTTP and unrelated owners', async () => {
  for (const url of [
    'https://alice.github.io.evil.example/game/', 'https://alice.github.io:443/game/',
    'https://password@alice.github.io/game/', 'http://alice.github.io/game/',
    'https://bob.github.io/game/', 'https://alice.github.io\\@evil.example/',
  ]) {
    const data = fixture([site(0, { url })]);
    await rejectCatalog(createCatalog(data.options).loadSites());
  }
});

test('index IDs are cross-checked against detail shards before returning a filtered result', async () => {
  const data = fixture([site(0, { agents: ['codex'] })]);
  data.files.get('v1/index.json')[0].id = 'different';
  await rejectCatalog(createCatalog(data.options).loadSites({ agent: 'codex' }));
});

test('failed shard reads recover, and returned nested arrays cannot poison cached data', async () => {
  const data = fixture([site(0, { agents: ['codex'], unknownSecret: 'do not expose' })]);
  const original = data.files.get('v1/0000.json');
  data.files.delete('v1/0000.json');
  const api = createCatalog(data.options);
  await rejectCatalog(api.loadSites());
  data.files.set('v1/0000.json', original);
  const first = await api.loadSites();
  assert.equal('unknownSecret' in first.sites[0], false);
  first.sites[0].agents.push('incorrect');
  assert.deepEqual((await api.loadSites()).sites[0].agents, ['codex']);
});

test('manifest refresh after thirty seconds switches to a new generation', async () => {
  const data = fixture([site(0)]);
  let now = 0;
  const api = createCatalog({ ...data.options, now: () => now });
  assert.equal((await api.loadSites()).sites[0].id, '0');
  const next = fixture([site(9)], 'v2');
  for (const [key, value] of next.files) data.files.set(key, value);
  now = 29_999;
  assert.equal((await api.loadSites()).sites[0].id, '0');
  now = 30_000;
  assert.equal((await api.loadSites()).sites[0].id, '9');
  assert.equal(data.calls.filter((key) => key === 'manifest.json').length, 2);
});

test('a deployment removing an old shard refreshes the manifest and restarts the entire page', async () => {
  const data = fixture();
  const api = createCatalog({ ...data.options, now: () => 0 });
  await api.loadSites({ limit: 1 }); // Cache v1's manifest and first shard.
  const next = fixture(Array.from({ length: 260 }, (_, i) => site(i + 1000)), 'v2');
  data.files.clear(); // A fresh Pages artifact has no previous generation.
  for (const [key, value] of next.files) data.files.set(key, value);
  const page = await api.loadSites({ offset: 254, limit: 5 });
  assert.equal(page.generation, 'v2');
  assert.deepEqual(page.sites.map((value) => value.id), ['1254', '1255', '1256', '1257', '1258']);
  assert.equal(data.calls.filter((key) => key === 'manifest.json').length, 2);
  assert.equal(data.calls.filter((key) => key === 'v1/0001.json').length, 1);
});

test('a missing old search index refreshes filters against the new deployment', async () => {
  const data = fixture([site(0)]);
  const api = createCatalog({ ...data.options, now: () => 0 });
  await api.loadStats();
  const next = fixture([
    site(10, { agents: ['claude'], signals: [{ term: 'play online', source: 'page' }] }),
    site(11, { agents: ['codex'], signals: [{ term: 'play online', source: 'page' }] }),
  ], 'v2');
  data.files.clear();
  for (const [key, value] of next.files) data.files.set(key, value);
  const page = await api.loadSites({ agent: 'codex', source: 'page', terms: ['play online'] });
  assert.equal(page.generation, 'v2');
  assert.equal(page.total, 1);
  assert.deepEqual(page.sites.map((value) => value.id), ['11']);
  assert.deepEqual(data.calls, ['manifest.json', 'v1/index.json', 'manifest.json', 'v2/index.json', 'v2/0000.json']);
});

test('random retries against the new catalog size after an old detail file disappears', async () => {
  const data = fixture();
  const api = createCatalog({ ...data.options, now: () => 0, fillRandomValues: (values) => { values[0] = 258; } });
  await api.loadStats();
  const next = fixture([site(40), site(41), site(42)], 'v2');
  data.files.clear();
  for (const [key, value] of next.files) data.files.set(key, value);
  assert.equal((await api.randomSite()).site.id, '40');
  assert.deepEqual(data.calls, ['manifest.json', 'v1/0001.json', 'manifest.json', 'v2/0000.json']);
});

test('a missing file in the unchanged generation fails without a retry loop', async () => {
  const data = fixture([site(0)]);
  data.files.delete('v1/0000.json');
  await rejectCatalog(createCatalog(data.options).loadSites());
  assert.deepEqual(data.calls, ['manifest.json', 'v1/0000.json', 'manifest.json']);
});

test('the shard LRU retains at most eight detail files', async () => {
  const data = fixture(Array.from({ length: 256 * 9 }, (_, i) => site(i)));
  const api = createCatalog(data.options);
  for (let index = 0; index < 9; index++) await api.loadSites({ offset: index * 256, limit: 1 });
  await api.loadSites({ offset: 0, limit: 1 });
  assert.equal(data.calls.filter((key) => key === 'v1/0000.json').length, 2);
  assert.equal(data.calls.filter((key) => key === 'manifest.json').length, 1);
});
