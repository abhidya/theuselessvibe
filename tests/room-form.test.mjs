import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('room code form uses valid HTML Unicode-set pattern and has an entry outside hidden mobile navigation', () => {
  const html = readFileSync(new URL('../site/room.html', import.meta.url), 'utf8');
  const pattern = html.match(/id="room-code"[^>]*pattern="([^"]+)"/)[1];
  const constraint = new RegExp(`^(?:${pattern})$`, 'v');
  assert.ok(constraint.test('pizza-1'));
  assert.ok(constraint.test('PIZZA'));
  assert.ok(!constraint.test('bad room'));
  assert.ok(!constraint.test('-bad'));
  assert.ok(!constraint.test('X'.repeat(25)));
  const index = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
  const hero = index.split('<section class="hero')[1].split('</section>')[0];
  assert.match(hero, /href="room\.html"[^>]*data-room-entry/);
});
