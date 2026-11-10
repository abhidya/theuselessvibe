import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const html = read('../site/index.html');
const js = read('../site/app.js');

test('solo and room use the same pinned framework and player geometry', () => {
  const room = read('../site/room.html');
  const bootstrap = /<link[^>]*href="https:\/\/cdn\.jsdelivr\.net\/npm\/bootstrap[^>]+>/;
  assert.equal(html.match(bootstrap)[0], room.match(bootstrap)[0]);
  assert.match(html, /href="rooms\/room\.css"/);
  assert.ok(read('../site/styles.css').length < 4000, 'Do not restore the legacy stack of CSS overrides');
});
test('Previous History Next have a separate navigation row, not a crowded header', () => {
  const nav = html.match(/<nav[^>]*aria-label="Game navigation"[^>]*>([\s\S]*?)<\/nav>/)[1];
  for (const id of ['player-previous', 'player-history', 'player-next']) assert.ok(nav.includes(`id="${id}"`));
  for (const id of ['player-star', 'player-report', 'player-direct']) assert.ok(!nav.includes(`id="${id}"`));
  assert.equal((nav.match(/<button/g) || []).length, 3);
});
test('GitHub star links are visible player-header actions with safe, announced new tabs', () => {
  for (const [source, id, dialogId] of [[html, 'player-star', 'player-more-dialog'], [read('../site/room.html'), 'star-repo', 'more-dialog']]) {
    const header = source.match(/<header class="room-header[\s\S]*?<\/header>/)[0];
    const link = header.match(new RegExp(`<a id="${id}"[^>]+>[^<]*<\\/a>`))?.[0];
    assert.ok(link, `${id} is present in the player header`);
    assert.match(link, /Star on GitHub/);
    assert.match(link, /target="_blank"/);
    assert.match(link, /rel="noopener noreferrer"/);
    assert.match(link, /aria-label="[^"]*opens in a new tab/);
    const more = source.match(new RegExp(`<dialog id="${dialogId}"[\\s\\S]*?<\\/dialog>`))[0];
    assert.ok(!more.includes(`id="${id}"`), 'Do not duplicate the star link in More');
  }
  const room = read('../site/rooms/page.js');
  assert.match(room, /\$\('star-repo'\)\.hidden = !view.site/);
});
test('secondary actions and history use native modal dialogs; help never covers iframe', () => {
  assert.match(html, /<dialog id="player-more-dialog"/);
  assert.match(html, /<dialog id="player-history-drawer"/);
  assert.doesNotMatch(html, /game-player-help|Codex|Claude/);
  assert.match(html, /Some games block embedding/);
  assert.match(js, /showModal\(\)/);
  assert.match(js, /replaceChildren\(frame\)/);
  assert.doesNotMatch(js, /allow-top-navigation/);
});
test('all literal application element IDs exist exactly once', () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  assert.equal(ids.length, new Set(ids).size);
  for (const [, id] of js.matchAll(/\$\('([^']+)'\)/g)) assert.ok(ids.includes(id), id);
});
test('blocked localStorage cannot prevent startup or play', () => {
  assert.match(js, /try \{ storage = window\.localStorage; \} catch/);
  assert.match(js, /createTrail\(storage\)/);
  assert.match(js, /addEventListener\('popstate', restoreNavigation\)/);
});
