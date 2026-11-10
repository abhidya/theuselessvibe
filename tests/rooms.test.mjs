import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { roomOrder, modulo, normalizeCode, clockPosition } from '../site/rooms/order.js';
import { createRoomSession, shareRoomUrl } from '../site/rooms/session.js';
import { loadRoomCatalog, EDITION_CHANGED } from '../site/rooms/catalog.js';

const entries = Array.from({ length: 12 }, (_, index) => ({ id: String(index), index, name: `Game ${index}`, repo: `demo/game-${index}` }));
const storage = () => { const values = new Map(); return { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }; };
const siteFor = async entry => ({ ...entry, url: `https://demo.github.io/game-${entry.id}/`, repoUrl: `https://github.com/${entry.repo}` });
const fixture = (options = {}) => createRoomSession({ loadCatalog: async () => ({ edition: 'v1', entries, siteFor }), ...options });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

test('same room code yields the same complete permutation regardless of index ordering', async () => {
  const a = await roomOrder(' pizza ', entries);
  const b = await roomOrder('PIZZA', [...entries].reverse());
  assert.deepEqual(a.map(x => x.id), b.map(x => x.id));
  assert.equal(new Set(a.map(x => x.id)).size, entries.length);
  assert.notDeepEqual(a.map(x => x.id), (await roomOrder('OTHER', entries)).map(x => x.id));
});

test('room hashing has a known stable vector, not Math.random or a device-time seed', async () => {
  const order = (await roomOrder('PIZZA', entries)).map(x => x.id);
  // A protocol change requires an explicit version, not silently changing existing rooms.
  assert.deepEqual(order, ["7", "8", "6", "2", "11", "5", "9", "10", "4", "0", "1", "3"]);
});

test('room codes and modulo validate input and wrap in both directions', () => {
  assert.equal(normalizeCode(' pizza-1 '), 'PIZZA-1');
  for (const code of ['', 'some room', '<script>', 'X'.repeat(25), '-NO']) assert.throws(() => normalizeCode(code));
  assert.equal(modulo(-1, 12), 11);
  assert.equal(modulo(12, 12), 0);
  assert.throws(() => modulo(1, 0));
  assert.throws(() => modulo(Infinity, 12));
  assert.equal(clockPosition(59_999, 12), 0);
  assert.equal(clockPosition(60_000, 12), 1);
  assert.equal(clockPosition(720_000, 12), 0);
});

test('manual joins start at zero and each device navigates independently in the same sequence', async () => {
  const a = fixture({ now: () => 100_000 });
  const b = fixture({ now: () => 900_000 });
  await Promise.all([a.join('PIZZA'), b.join('pizza')]);
  assert.equal(a.state.auto, false);
  assert.equal(b.state.auto, false);
  assert.equal(a.state.site.id, b.state.site.id);
  await a.move(1);
  assert.equal(b.state.position, 0);
  await b.move(1);
  assert.equal(a.state.site.id, b.state.site.id);
  await a.move(-1);
  assert.equal(a.state.position, 0);
  await a.move(-1);
  assert.equal(a.state.position, 11);
  await a.move(1);
  assert.equal(a.state.position, 0);
});

test('a manual session never advances merely because time passes', async () => {
  let now = 0;
  const session = fixture({ now: () => now });
  await session.join('PIZZA');
  now = 900_000;
  await session.tick();
  assert.equal(session.state.position, 0);
  assert.equal(session.state.auto, false);
});

test('auto-rotate aligns different join times and catches up after a suspended tab', async () => {
  let nowA = 60_001, nowB = 119_999;
  const a = fixture({ now: () => nowA }), b = fixture({ now: () => nowB });
  await a.join('PIZZA');
  await b.join('PIZZA');
  await Promise.all([a.setAuto(true), b.setAuto(true)]);
  assert.equal(a.state.site.id, b.state.site.id);
  assert.equal(a.state.position, 1);
  nowA = nowB = 605_000;
  await Promise.all([a.tick(), b.tick()]);
  assert.equal(a.state.position, 10);
  assert.equal(a.state.site.id, b.state.site.id);
});

test('turning rotation off keeps the current game; Previous, Next and history disable it', async () => {
  let now = 120_000;
  const session = fixture({ now: () => now });
  await session.join('PIZZA');
  await session.setAuto(true);
  const game = session.state.site.id;
  await session.setAuto(false);
  now = 300_000;
  await session.tick();
  assert.equal(session.state.site.id, game);
  await session.setAuto(true);
  await session.move(1);
  assert.equal(session.state.auto, false);
  assert.equal(session.state.position, 6);
  await session.setAuto(true);
  await session.move(-1);
  assert.equal(session.state.auto, false);
  await session.setAuto(true);
  await session.go(1);
  assert.equal(session.state.position, 1);
  assert.equal(session.state.auto, false);
});

test('refresh restores position/history per room and edition, never the auto toggle', async () => {
  const disk = storage();
  const a = fixture({ storage: disk, now: () => 420_000 });
  await a.join('PIZZA');
  await a.move(1);
  await a.setAuto(true);
  const b = fixture({ storage: disk });
  await b.join('PIZZA');
  assert.equal(b.state.position, 7);
  assert.equal(b.state.auto, false);
  assert.deepEqual(b.state.history.map(x => x.position), [0,1,7]);
  await b.join('OTHER');
  assert.equal(b.state.position, 0);
  const c = fixture({ storage: disk, loadCatalog: async () => ({ edition: 'v2', entries, siteFor }) });
  await c.join('PIZZA');
  assert.equal(c.state.position, 0);
});

test('corrupt/disabled storage does not stop play, and local history is bounded', async () => {
  for (const disk of [null, { getItem: () => '{broken', setItem() {} }, { getItem() { throw Error(); }, setItem() { throw Error(); } }]) {
    const session = fixture({ storage: disk });
    await session.join('PIZZA');
    assert.ok(session.state.site);
  }
  const session = fixture({ storage: storage() });
  await session.join('PIZZA');
  for (let i = 0; i < 110; i++) await session.move(1);
  assert.equal(session.state.history.length, 100);
});

test('two simultaneous joins cannot publish the older room after the newer one', async () => {
  const pending = deferred();
  let calls = 0;
  const session = fixture({ loadCatalog: async () => (++calls === 1 ? pending.promise : { edition: 'v2', entries, siteFor }) });
  const first = session.join('OLD');
  await session.join('NEW');
  pending.resolve({ edition: 'v1', entries, siteFor });
  await first;
  assert.equal(session.state.code, 'NEW');
  assert.equal(session.state.edition, 'v2');
});

test('leaving cancels an in-flight selection and does not reopen the frame', async () => {
  const pending = deferred();
  let delay = false;
  const session = fixture({ loadCatalog: async () => ({ edition: 'v1', entries, siteFor: entry => delay ? pending.promise : siteFor(entry) }) });
  await session.join('PIZZA');
  delay = true;
  const next = session.move(1);
  session.leave();
  pending.resolve(await siteFor(entries[0]));
  await next;
  assert.equal(session.state.site, null);
  assert.equal(session.state.code, '');
  assert.equal(session.state.auto, false);
});

test('a minute change during a fetch never commits the old automatic slot', async () => {
  let now = 60_000, delay = false;
  const pending = deferred();
  const seen = [];
  const session = fixture({ now: () => now, onChange: state => { if (!state.busy && state.site) seen.push(state.position); }, loadCatalog: async () => ({ edition: 'v1', entries, siteFor: async entry => {
    if (delay) { delay = false; await pending.promise; }
    return siteFor(entry);
  } }) });
  await session.join('PIZZA');
  delay = true;
  const change = session.setAuto(true);
  now = 120_000;
  pending.resolve();
  await change;
  assert.equal(session.state.position, 2);
  assert.deepEqual(seen, [0,2]);
});

test('share links include room and edition, never local position, auto, or other URL parameters', () => {
  const link = new URL(shareRoomUrl('https://example.com/site/room.html?step=7&auto=true&other=secret#history', 'pizza', 'v1'));
  assert.equal(link.pathname, '/site/room.html');
  assert.equal(link.search, '?room=PIZZA&v=v1');
  assert.equal(link.hash, '');
});

function catalogFixture() {
  const calls = [];
  const adapter = {
    loadStats: async () => ({ generation: 'v1', eligibleSites: entries.length }),
    loadSites: async options => { calls.push(options); return { generation: 'v1', sites: [await siteFor(entries[options.offset])] }; },
  };
  const fetcher = async () => new Response(JSON.stringify(entries));
  return { adapter, fetcher, calls };
}

test('catalog loads only its index up front and uses unfiltered lazy validated detail fetches', async () => {
  const fixture = catalogFixture();
  const catalog = await loadRoomCatalog(fixture);
  assert.equal(fixture.calls.length, 0);
  const site = await catalog.siteFor(catalog.entries[4]);
  assert.equal(site.id, '4');
  assert.deepEqual(fixture.calls, [{ offset: 4, limit: 1, order: 'index' }]);
});

test('expired share editions and mid-session catalog changes fail explicitly rather than reshuffle', async () => {
  const fixture = catalogFixture();
  await assert.rejects(loadRoomCatalog({ ...fixture, edition: 'v0' }), { message: EDITION_CHANGED });
  const catalog = await loadRoomCatalog(fixture);
  fixture.adapter.loadSites = async () => ({ generation: 'v2', sites: [await siteFor(entries[0])] });
  await assert.rejects(catalog.siteFor(entries[0]), { message: EDITION_CHANGED });
});

test('empty, malformed, duplicate, missing or mismatched room catalogs are rejected', async () => {
  const fixture = catalogFixture();
  await assert.rejects(loadRoomCatalog({ ...fixture, edition: '../escape' }));
  for (const values of [[], {}, entries.slice(1), entries.map((row, i) => ({ ...row, id: 'duplicate' })), entries.map(row => ({ ...row, index: 99 }))]) {
    await assert.rejects(loadRoomCatalog({ ...fixture, fetcher: async () => new Response(JSON.stringify(values)) }));
  }
  await assert.rejects(loadRoomCatalog({ ...fixture, fetcher: async () => new Response('Not found', { status: 404 }) }));
  await assert.rejects(loadRoomCatalog({ ...fixture, adapter: { loadStats: async () => ({ generation: 'v1', eligibleSites: 0 }) } }));
});

test('room UI keeps three navigation buttons separate and auto-rotate unchecked by default', () => {
  const html = readFileSync(new URL('../site/room.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../site/rooms/room.css', import.meta.url), 'utf8');
  const page = readFileSync(new URL('../site/rooms/page.js', import.meta.url), 'utf8');
  for (const [, id] of page.matchAll(/\$\('([^']+)'\)/g)) assert.ok(html.includes(`id="${id}"`), id);
  assert.match(html, /bootstrap@5\.3\.8/);
  assert.doesNotMatch(html.match(/<input id="auto-rotate"[^>]+>/)[0], /checked/);
  const nav = html.match(/<nav class="room-nav[\s\S]*?<\/nav>/)[0];
  assert.equal((nav.match(/<button/g) || []).length, 3);
  assert.match(css, /repeat\(3, minmax\(0, 1fr\)\)/);
  assert.doesNotMatch(html, /Codex|Claude|Esc closes|game-player-help/);
  assert.match(page, /visibilitychange/);
  assert.match(page, /popstate/);
});
