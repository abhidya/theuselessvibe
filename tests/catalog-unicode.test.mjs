import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtemp, rm, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {isText} from '../site/text.js';
import {parseMetadata} from '../site/metadata.js';
import {checkBrowserCatalog} from '../scripts/check_browser_catalog.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const evidence = (excerpt) => ({field: 'genres', value: 'puzzle', rule: 'readme', strength: 'declared',
  path: 'README.md', excerpt, url: 'https://github.com/fixture/game-1'});

test('catalog text limits use Unicode code points, not UTF-16 units or graphemes', () => {
  for (const text of ['x', '🎮', '𠮷']) {
    assert(isText(text.repeat(160), 160));
    assert(!isText(text.repeat(161), 160));
  }
  assert(isText('🇬🇷'.repeat(80), 160)); // Two code points per flag.
  assert(!isText('🇬🇷'.repeat(81), 160));
  assert(isText('e\u0301'.repeat(80), 160)); // Combining marks count separately, as in Python.
  assert(!isText('e\u0301'.repeat(81), 160));
  assert(!isText('', 160)); assert(isText('', 160, true));
  assert(!isText(null, 160)); assert(!isText({}, 160));
});

test('180-code-point evidence with emoji is accepted, 181 is still rejected', () => {
  const excerpt = '🎮' + 'a'.repeat(179);
  assert.equal(excerpt.length, 181);
  const parsed = parseMetadata({schemaVersion: 1, evidence: [evidence(excerpt)]}, 'fixture/game-1');
  assert.equal(parsed.evidence[0].excerpt, excerpt);
  assert.throws(() => parseMetadata({schemaVersion: 1, evidence: [evidence(excerpt + 'a')]}));
});

test('Unicode acceptance does not relax evidence URL trust boundaries', () => {
  for (const url of ['javascript:alert(1)', 'https://github.com.evil.test/fixture/game-1', 'https://github.com/other/game']) {
    assert.throws(() => parseMetadata({schemaVersion: 1, evidence: [{...evidence('🎮'), url}]}, 'fixture/game-1'));
  }
});

// The Python producer, the full compact index, and every detail shard must agree.
// Synthetic fixtures never enter site/data or the production catalog.
const EXPORT = String.raw`
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from export_catalog import export_catalog
count = int(sys.argv[2])
now = '2026-11-08T00:00:00Z'
sites = []
for i in range(count):
    repo = f'fixture/game-{i}'
    sites.append(dict(id=str(i), repo=repo, name=f'Game {i}', description='🎮' * 160,
        repoUrl=f'https://github.com/{repo}', url=f'https://fixture.github.io/game-{i}/',
        stars=0, license='MIT', createdAt=now, updatedAt=now, checkedAt=now,
        agents=[], signals=[dict(term='play online', source='page')], agentEvidence=[],
        metadata=dict(schemaVersion=1, genres=['puzzle'], evidence=[dict(field='genres', value='puzzle',
            rule='readme', strength='declared', path='README.md', excerpt='🎮' * 180,
            url=f'https://github.com/{repo}')]), attributionStatus='pending'))
export_catalog(sites, dict(query='fixture', searchUrl='https://github.com/search?q=test',
    generatedAt=now, scannedRepositories=count, matchedRepositories=count, complete=True), Path(sys.argv[1]))
`;

test('2,500 Python-exported Unicode games load through the production browser adapter', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'vibe-contract-'));
  try {
    execFileSync('python3', ['-c', EXPORT, directory, '2500'], {cwd: ROOT});
    assert.equal((await checkBrowserCatalog(directory)).sites, 2500);
    const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json')));
    const shardFile = path.join(directory, manifest.shards.at(-1).key);
    const shard = JSON.parse(await readFile(shardFile));
    shard.at(-1).url = 'https://unrelated.example/game';
    await writeFile(shardFile, JSON.stringify(shard));
    await assert.rejects(checkBrowserCatalog(directory), /Browser catalog rejected page/);
  } finally { await rm(directory, {recursive: true, force: true}); }
});

test('pre-publish check also supports an empty exported catalog', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'vibe-empty-'));
  try {
    execFileSync('python3', ['-c', EXPORT, directory, '0'], {cwd: ROOT});
    assert.equal((await checkBrowserCatalog(directory)).sites, 0);
  } finally { await rm(directory, {recursive: true, force: true}); }
});
