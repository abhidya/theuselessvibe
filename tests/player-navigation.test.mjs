import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { MAX_TRAIL, STORAGE_KEY, safeEntry } from '../site/trail.js';

const DATE = '2026-11-07T00:00:00Z';
const SITES = ['A', 'B', 'C', 'D'].map((id, index) => ({
  id, repo: `fixture/game-${id}`, name: `Game ${id}`, description: 'Navigation fixture',
  url: `https://fixture.github.io/game-${id}/`, repoUrl: `https://github.com/fixture/game-${id}`,
  stars: [1, 100, 0, null][index], starsCheckedAt: index === 3 ? null : DATE,
  license: 'MIT', createdAt: DATE, updatedAt: DATE, checkedAt: DATE,
}));
const MANIFEST = {
  schemaVersion: 1, generation: 'navigation-test', query: '',
  searchUrl: 'https://github.com/search?q=game', generatedAt: DATE,
  matchedRepositories: 4, scannedRepositories: 4, eligibleSites: 4, complete: true,
  shardSize: 256, indexKey: 'navigation-test/index.json',
  shards: [{key: 'navigation-test/0000.json', count: 4}],
};
const html = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
const globalNames = ['window', 'document', 'location', 'history', 'fetch'];
const originalGlobals = new Map(globalNames.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
afterEach(() => {
  for (const [name, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});
let imports = 0;

// A deliberately small DOM/history double drives the complete production app
// through its registered click/popstate handlers. This is not a layout test.
async function application({ blockedStorage = false, disk = new Map(), navigation = null } = {}) {
  const elements = new Map();
  class Target {
    listeners = new Map();
    addEventListener(type, listener) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(listener);
    }
    dispatch(type, properties = {}) {
      const event = { type, target: this, button: 0, preventDefault() {}, ...properties };
      for (const listener of this.listeners.get(type) || []) listener(event);
    }
  }
  class Element extends Target {
    children = [];
    attributes = new Map();
    classList = { add() {}, remove() {} };
    open = false;
    hidden = false;
    disabled = false;
    value = '';
    _text = '';
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this._text = ''; this.children = children; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatch('close'); }
    focus() {}
    reportValidity() { return true; }
    click() { assert.equal(this.disabled, false, `Cannot click disabled ${this.id || 'element'}`); this.dispatch('click'); }
  }
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) new Element().id = id;
  const $ = id => elements.get(id);
  const window = new Target();
  Object.defineProperty(window, 'localStorage', { get() {
    if (blockedStorage) throw new Error('Storage disabled');
    return { getItem: key => disk.get(key) ?? null, setItem: (key, value) => disk.set(key, value) };
  } });
  const document = {
    body: new Element(), getElementById: $, querySelectorAll: () => [],
    createElement: () => new Element(), createTextNode: textContent => ({textContent}),
  };
  navigation ??= { entries: [{state: null, url: 'https://directory.github.io/'}], cursor: 0 };
  const location = new URL(navigation.entries[navigation.cursor].url);
  const history = {
    get state() { return structuredClone(navigation.entries[navigation.cursor].state); },
    pushState(state, _, href) {
      location.href = new URL(href, location).href;
      navigation.entries.splice(++navigation.cursor, Infinity, {state: structuredClone(state), url: location.href});
    },
    go(delta) {
      const cursor = navigation.cursor + delta;
      assert.ok(cursor >= 0 && cursor < navigation.entries.length);
      navigation.cursor = cursor;
      location.href = navigation.entries[cursor].url;
      window.dispatch('popstate', {state: history.state});
    },
  };
  const fetch = async href => {
    const path = new URL(href).pathname;
    if (path.endsWith('/manifest.json')) return Response.json(MANIFEST);
    if (path.endsWith('/navigation-test/index.json')) return Response.json(SITES.map((site, index) => ({
      index, id: site.id, repo: site.repo, name: site.name, description: site.description,
      stars: site.stars, starsCheckedAt: site.starsCheckedAt,
    })));
    if (path.endsWith('/navigation-test/0000.json')) return Response.json(SITES);
    throw new Error(`Unexpected catalog request: ${href}`);
  };
  for (const [name, value] of Object.entries({window, document, location, history, fetch})) {
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
  }
  await import(`../site/app.js?navigation-test=${++imports}`);
  for (let attempt = 0; attempt < 100 && $('site-grid').children.length !== SITES.length; attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal($('site-grid').children.length, SITES.length, 'Production app initialized its catalog');
  const descendants = element => [element, ...element.children.flatMap(descendants)];
  return {
    $, window, history, location, navigation, disk, blockedStorage, descendants,
    play(id) {
      if ($('game-player').open) $('player-close').click();
      const link = descendants($('site-grid')).find(element =>
        element.className?.includes('card-site-link') && element.textContent === `Game ${id}`);
      assert.ok(link, `Catalog card for ${id}`);
      link.click();
    },
    expectGame(id) {
      assert.equal($('game-player').open, true);
      assert.equal($('player-name').textContent, `Game ${id}`);
      assert.equal($('solo-frame-host').children[0].src, SITES.find(site => site.id === id).url);
      assert.equal(location.searchParams.get('play'), id);
      assert.equal($('player-star').href, SITES.find(site => site.id === id).repoUrl, 'Star follows the currently selected repository');
    },
    expectHome() {
      assert.equal($('game-player').open, false);
      assert.equal($('solo-frame-host').children.length, 0);
    },
  };
}

test('Browse cards show collected counts and unknown values in descending popularity order', async () => {
  const app = await application();
  const cards = app.$('site-grid').children;
  const titles = cards.map(card => app.descendants(card).find(element => element.className?.includes('card-site-link')).textContent);
  assert.deepEqual(titles, ['Game B', 'Game A', 'Game C', 'Game D']);
  const counts = cards.map(card => app.descendants(card).find(element => element.className?.includes('card-stars')));
  assert.deepEqual(counts.map(element => element.textContent), [
    '100 GitHub stars (last collected)', '1 GitHub star (last collected)',
    '0 GitHub stars (last collected)', 'GitHub stars: unknown',
  ]);
  assert.equal(counts[0].title, 'Star count collected 2026-11-07T00:00:00.000Z');
  assert.match(cards[0].textContent, /Star count collected: 2026-11-07T00:00:00.000Z. Counts are not live./);
  assert.doesNotMatch(cards[3].textContent, /Star count collected:/);
  assert.match(app.$('updated-at').textContent, /Most-starred games appear first, using the latest collected counts/);
});

test('native Back and Forward restore games removed by a new solo branch', async () => {
  const app = await application();
  for (const id of ['A', 'B', 'C']) app.play(id);
  app.$('player-previous').click();
  app.expectGame('B');
  const revisitedState = app.history.state;
  app.play('D');
  assert.deepEqual(JSON.parse(app.disk.get(STORAGE_KEY)).entries.map(site => site.id), ['A', 'B', 'D']);

  app.history.go(-1); app.expectHome();
  app.history.go(-1); app.expectGame('B');
  app.history.go(-1); app.expectGame('C');
  app.history.go(1); app.expectGame('B');
  app.history.go(1); app.expectHome();
  app.history.go(1); app.expectGame('D');
  assert.deepEqual(app.history.state.site, safeEntry(SITES[3]), 'New visits store only validated trail fields');
  assert.deepEqual(revisitedState.site, safeEntry(SITES[1]), 'Trail navigation also stores its own snapshot');
});

test('native history restores an evicted game without exceeding the trail limit', async () => {
  const app = await application();
  app.play('A');
  const first = app.navigation.cursor;
  for (let index = 0; index < MAX_TRAIL; index++) app.play(index % 2 ? 'C' : 'B');
  const saved = JSON.parse(app.disk.get(STORAGE_KEY));
  assert.equal(saved.entries.length, MAX_TRAIL);
  assert.equal(saved.entries.some(site => site.id === 'A'), false);
  app.history.go(first - app.navigation.cursor);
  app.expectGame('A');
  const restored = JSON.parse(app.disk.get(STORAGE_KEY));
  assert.ok(restored.entries.length <= MAX_TRAIL);
  assert.equal(restored.entries[restored.cursor].id, 'A');
});

test('refresh can restore a browser snapshot even when localStorage is unavailable', async () => {
  const first = await application({blockedStorage: true});
  first.play('A');
  const refreshed = await application(first);
  refreshed.expectGame('A');
  refreshed.$('player-history').click();
  assert.equal(refreshed.$('player-history-list').children.length, 1);
});

test('invalid, mismatched, or unowned browser snapshots cannot restore a game', async () => {
  const app = await application();
  const state = {uselessVibe: 1, id: 'A', cursor: 0, site: safeEntry(SITES[0])};
  for (const invalid of [
    {...state, site: null},
    {...state, site: {...state.site, url: 'https://other.github.io/game-A/'}},
    {...state, site: {...state.site, repoUrl: 'https://github.com/other/game-A'}},
    {...state, site: {...state.site, id: 'B'}},
    {...state, id: null},
    {...state, id: 1},
    {...state, uselessVibe: 2},
    {id: 'A', site: state.site},
  ]) {
    app.history.pushState(invalid, '', '?play=A');
    app.window.dispatch('popstate', {state: app.history.state});
    app.expectHome();
    assert.equal(app.disk.has(STORAGE_KEY), false, 'Invalid snapshots must not be persisted');
  }
});

test('legacy browser states still use the trail and invalid cursors fall back to the ID', async () => {
  const app = await application();
  app.play('A'); app.play('B');
  for (const state of [{uselessVibe: 1, id: 'A', cursor: 0}, {uselessVibe: 1, id: 'A', cursor: '0'}, null]) {
    app.history.pushState(state, '', '?play=A');
    app.window.dispatch('popstate', {state: app.history.state});
    app.expectGame('A');
  }
});
