#!/usr/bin/env node
/** Validate exported JSON through the exact browser catalog adapter before Pages
 * publication. This is a data-contract check, not a claim of rendered UI QA. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createCatalog} from '../site/catalog.js';

export async function checkBrowserCatalog(directory) {
  const root = path.resolve(directory);
  const origin = 'https://catalog.invalid';
  const prefix = '/data/';
  const fetcher = async (input) => {
    const url = new URL(input);
    assert.equal(url.origin, origin, 'Unexpected catalog origin');
    assert(url.pathname.startsWith(prefix), 'Unexpected catalog path');
    const filename = path.resolve(root, decodeURIComponent(url.pathname.slice(prefix.length)));
    assert(filename.startsWith(root + path.sep), 'Catalog path escapes its directory');
    try {
      const bytes = await readFile(filename);
      return new Response(bytes, {headers: {'content-type': 'application/json'}});
    } catch (error) {
      if (error.code === 'ENOENT') return new Response('', {status: 404});
      throw error;
    }
  };
  const manifest = JSON.parse(await readFile(path.join(root, 'manifest.json'), 'utf8'));
  const catalog = createCatalog({baseUrl: origin + prefix, fetcher});
  const stats = await catalog.loadStats();
  // Every entry in the compact index is parsed here, including metadata.
  await catalog.loadFacets();
  // Read via the same checked path as the adapter; do not trust indexKey as a path.
  const response = await fetcher(origin + prefix + manifest.indexKey);
  assert(response.ok, 'Missing compact index');
  const index = await response.json();
  const ids = new Set();
  for (let offset = 0; offset < stats.eligibleSites; offset += 50) {
    try {
      const page = await catalog.loadSites({offset, limit: 50});
      assert.equal(page.total, stats.eligibleSites);
      assert.equal(page.sites.length, Math.min(50, stats.eligibleSites - offset));
      for (const [n, site] of page.sites.entries()) {
        assert.equal(site.id, index[offset + n].id, `Index/detail mismatch at ${offset + n}`);
        assert(!ids.has(site.id), `Duplicate site ${site.id}`);
        ids.add(site.id);
      }
    } catch (error) {
      throw new Error(`Browser catalog rejected page at offset ${offset}: ${error.message}`, {cause: error});
    }
  }
  assert.equal(ids.size, stats.eligibleSites);
  // Exercise the index-backed path even when metadata has not yet been enriched.
  await catalog.loadSites({source: 'page', limit: 1});
  await catalog.loadSites({query: 'game', limit: 1});
  await catalog.loadSites({genre: 'unknown', limit: 1});
  if (stats.eligibleSites > 0) await catalog.randomSite();
  console.log(`Browser catalog contract passed: ${ids.size} sites, all index entries and detail shards.`);
  return {sites: ids.size, generation: stats.generation};
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  checkBrowserCatalog(process.argv[2] || 'site/data').catch(error => {
    console.error(error.stack);
    process.exitCode = 1;
  });
}
