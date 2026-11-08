import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseMetadata, metadataFilters, matchesMetadata, facetCounts} from '../site/metadata.js';
import {createCatalog} from '../site/catalog.js';
const DATE = '2026-11-07T00:00:00Z';
const metadata = (extras = {}) => ({schemaVersion: 1, genres: ['puzzle'], modes: ['solo'], languages: ['JavaScript'], ...extras});
function site(id, extras = {}) { return {id, repo:`alice/game-${id}`, name:`Game ${id}`, description:'Synthetic test fixture',
  url:`https://alice.github.io/game-${id}/`, repoUrl:`https://github.com/alice/game-${id}`, stars:0, license:'MIT',
  updatedAt:DATE, createdAt:DATE, checkedAt:DATE, agents:[], signals:[], agentEvidence:[], ...extras}; }
function catalog(sites) {
  const files = new Map([
    ['manifest.json',{schemaVersion:1,generation:'tags',query:'fixtures',searchUrl:'https://github.com/search?q=test',
      generatedAt:DATE,matchedRepositories:sites.length,scannedRepositories:sites.length,eligibleSites:sites.length,
      complete:true,shardSize:256,indexKey:'tags/index.json',shards:[{key:'tags/0000.json',count:sites.length}],terms:[],agentCounts:{},termCounts:{}}],
    ['tags/index.json',sites.map((s,index) => ({index,id:s.id,repo:s.repo,name:s.name,description:s.description,
      terms:[],agents:[],pageTerms:[],readmeTerms:[],metadata:s.metadata,createdAt:s.createdAt,updatedAt:s.updatedAt,checkedAt:s.checkedAt}))],
    ['tags/0000.json',sites],
  ]);
  return createCatalog({baseUrl:'https://test.example/data/',fillRandomValues: a => {a[0]=0;},
    fetcher: async url => new Response(JSON.stringify(files.get(new URL(url).pathname.replace('/data/',''))),{status:200})});
}

test('old catalogs have unknown metadata without disappearing', () => {
  const value=parseMetadata(undefined);
  assert.deepEqual(value.genres,[]); assert.equal(value.playerCount,null); assert.equal(value.sourceStatus,'pending');
  assert.ok(value.unknownFields.includes('genres'));
});
test('unsafe or wrong-repository evidence links and malformed metadata are rejected', () => {
  const evidence={field:'genres',value:'puzzle',rule:'topic',strength:'declared',path:'$repository',excerpt:'puzzle-game',url:'https://github.com/alice/game-1'};
  for (const url of ['javascript:alert(1)','https://github.com.evil.test/a','https://github.com/bob/other'])
    assert.throws(() => parseMetadata(metadata({evidence:[{...evidence,url}]}),'alice/game-1'));
  assert.throws(() => parseMetadata(metadata({genres:'puzzle'})));
  assert.throws(() => parseMetadata(metadata({playerCount:{min:4,max:2}})));
});
test('facet filters intersect and Unknown means absent evidence, not solo', () => {
  const entry={metadata:parseMetadata(metadata()),updatedAt:DATE};
  assert.equal(matchesMetadata(entry,metadataFilters({genre:'puzzle',mode:'solo',language:'javascript'})),true);
  assert.equal(matchesMetadata(entry,metadataFilters({genre:'racing'})),false);
  assert.equal(matchesMetadata(entry,metadataFilters({mode:'unknown'})),false);
  assert.equal(matchesMetadata({metadata:parseMetadata()},metadataFilters({mode:'unknown'})),true);
  assert.equal(matchesMetadata({metadata:parseMetadata()},metadataFilters({mode:'solo'})),false);
});
test('generic multiplayer never matches the online-only filter', () => {
  const entry={metadata:parseMetadata(metadata({modes:['multiplayer-unspecified']}))};
  assert.equal(matchesMetadata(entry,metadataFilters({mode:'online-multiplayer'})),false);
  assert.equal(matchesMetadata(entry,metadataFilters({mode:'multiplayer'})),true);
});
test('declared player range and UTC calendar dates filter correctly', () => {
  const entry={metadata:parseMetadata(metadata({playerCount:{min:2,max:4}})),createdAt:DATE,updatedAt:DATE,checkedAt:DATE};
  assert.equal(matchesMetadata(entry,metadataFilters({players:4,updatedAfter:'2026-11-07'})),true);
  assert.equal(matchesMetadata(entry,metadataFilters({players:5})),false);
  assert.equal(matchesMetadata(entry,metadataFilters({updatedAfter:'2026-11-08'})),false);
  assert.equal(matchesMetadata(entry,metadataFilters({contentLanguage:'unknown'})),true);
  assert.throws(() => metadataFilters({players:0})); assert.throws(() => metadataFilters({players:1.5}));
  assert.throws(() => metadataFilters({updatedAfter:'2026-02-31'}));
});
test('facet counts include unknown records and do not conflate languages', () => {
  const values=[{metadata:parseMetadata(metadata({contentLanguages:['fr']}))},{metadata:parseMetadata()}];
  const counts=facetCounts(values);
  assert.equal(counts.languages.JavaScript,1); assert.equal(counts.languages.unknown,1);
  assert.equal(counts.contentLanguages.fr,1); assert.equal(counts.modes.solo,1);
});
test('Browse and Random use the same structured metadata filter set', async () => {
  const api=catalog([site('1',{metadata:metadata()}),site('2',{metadata:metadata({genres:['racing'],modes:['online-multiplayer']})}),site('3')]);
  const filters={genre:'racing',mode:'online-multiplayer',updatedAfter:'2026-11-01'};
  const page=await api.loadSites(filters); const selected=await api.randomSite(filters);
  assert.equal(page.total,1); assert.equal(page.sites[0].id,'2'); assert.equal(selected.site.id,'2');
  const unknown=await api.loadSites({genre:'unknown'}); assert.equal(unknown.total,1); assert.equal(unknown.sites[0].id,'3');
  assert.equal((await api.loadSites()).total,3);
});
test('filter facet counts load through the existing immutable catalog adapter', async () => {
  const api=catalog([site('1',{metadata:metadata()}),site('2')]);
  const result=await api.loadFacets(); assert.equal(result.generation,'tags'); assert.equal(result.facets.genres.puzzle,1);
  assert.equal(result.facets.genres.unknown,1);
});
test('history, room controls and iframe sandbox are not replaced by metadata UI', () => {
  const html=readFileSync(new URL('../site/index.html',import.meta.url),'utf8');
  for (const id of ['metadata-filters','player-previous','player-history','player-next','player-more','filter-genre','filter-mode','filter-language','filter-players']) assert.ok(html.includes(`id="${id}"`),id);
  assert.ok(html.includes('href="room.html"')); assert.ok(html.includes('sandbox='));
  const app=readFileSync(new URL('../site/app.js',import.meta.url),'utf8');
  assert.match(app,/randomSite\(filters\(\)\)/); assert.match(app,/loadSites\(\{ \.\.\.filters\(\)/);
  assert.doesNotMatch(html,/Codex|Claude/);
});
