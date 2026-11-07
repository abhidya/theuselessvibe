import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
const js = readFileSync(new URL('../site/app.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../site/styles.css', import.meta.url), 'utf8');

test('embedded game player has always-visible next, close, star, report and direct link controls', () => {
  for (const id of ['game-player', 'player-frame', 'player-next', 'player-close', 'player-star', 'player-report', 'player-direct']) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  assert.match(html, /class="game-player-next"/);
  assert.match(css, /\.game-player-next\s*\{[^}]*background:#f03b3b/);
  assert.match(css, /\.game-player\s*\{[^}]*position:fixed/);
});

test('random discovery and directory clicks stay inside the site', () => {
  assert.match(js, /\$\("player-next"\)\.addEventListener\("click", \(\) =>/);
  assert.match(js, /visit\.addEventListener\("click"/);
  assert.match(js, /navigateToGame\(site\)/);
  assert.doesNotMatch(js, /window\.open\("about:blank"/);
});

test('embed has limited permissions and a direct-open fallback for sites blocking iframes', () => {
  assert.match(html, /sandbox="allow-scripts [^"]+"/);
  assert.doesNotMatch(html, /allow-top-navigation/);
  assert.match(html, /Some games block embedding/);
  assert.match(js, /\$\("player-frame"\)\.removeAttribute\("src"\)/);
  assert.match(js, /event\.key === "Escape"/);
});
