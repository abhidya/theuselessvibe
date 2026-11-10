"""Metadata is evidence, not a runtime certification. Fixtures never enter the catalog."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from game_metadata import classify, candidate_files, VERSION, MAX_FILES, MAX_FILE_BYTES
from collect import Store, step_check, step_metadata, step_collection, BudgetExpired, HttpFailure, export, source_key
from test_collector import CONFIG, FakeClient, repository


class TagRulesTests(unittest.TestCase):
    def test_reproducible_under_topic_and_file_order_changes(self):
        repo = repository(topics=['puzzle-game', 'single-player'], language='JavaScript')
        files = {'package.json':'{"dependencies":{"phaser":"3.90.0"}}', 'index.html':'<html lang="en">'}
        first = classify(repo, 'Controls: arrow keys', files=files, observed_at='2026-11-07T00:00:00Z')
        repo['topics'].reverse()
        self.assertEqual(first, classify(repo, 'Controls: arrow keys', files=dict(reversed(list(files.items()))), observed_at='2026-11-07T00:00:00Z'))

    def test_topics_and_explicit_genres_not_random_words(self):
        self.assertEqual(classify(repository(), 'A puzzle documentation tool')['genres'], [])
        tags = classify(repository(topics=['puzzle-game']), '**Genre:** strategy, tower defense')
        self.assertEqual(tags['genres'], ['puzzle','strategy','tower-defense'])
        self.assertTrue(all(e['strength'] == 'declared' for e in tags['evidence']))

    def test_language_is_not_content_language(self):
        tags = classify(repository(language='TypeScript'), files={'index.html':'<!doctype html><html lang="fr-CA">'})
        self.assertEqual(tags['languages'], ['TypeScript'])
        self.assertEqual(tags['contentLanguages'], ['fr-ca'])

    def test_generic_multiplayer_does_not_imply_online(self):
        tags = classify(repository(), 'A multiplayer game.')
        self.assertEqual(tags['modes'], ['multiplayer-unspecified'])
        self.assertEqual(classify(repository(), 'Local multiplayer with arrow keys')['modes'], ['local-multiplayer'])

    def test_negative_future_and_code_blocks_are_not_positive_tags(self):
        for text in ('No multiplayer', 'Online multiplayer is not supported', 'Online multiplayer coming soon',
                     '# Roadmap\nOnline multiplayer\n## Planned\nTouch controls',
                     '```md\nOnline multiplayer\n```', '> Online multiplayer', '<!-- Online multiplayer -->',
                     '- [ ] online multiplayer', "We don't support controllers"):
            tags = classify(repository(), text)
            self.assertFalse(tags['modes'], text)
            self.assertFalse(tags['controls'], text)

    def test_readme_section_can_resume_current_features(self):
        tags = classify(repository(), '# Roadmap\nOnline multiplayer\n# Controls\nUse your keyboard')
        self.assertFalse(tags['modes']); self.assertEqual(tags['controls'], ['keyboard'])

    def test_networking_dependency_is_not_multiplayer_or_genre(self):
        tags = classify(repository(), files={'package.json':'{"dependencies":{"socket.io":"4","three":"1"}}'})
        self.assertEqual(tags['libraries'], ['threejs']); self.assertFalse(tags['engines'])
        self.assertFalse(tags['modes']); self.assertFalse(tags['genres'])

    def test_engine_is_dependency_evidence_not_runtime_certification(self):
        tags = classify(repository(), files={'package.json':'{"dependencies":{"phaser":"3"}}'}, revision='a'*40)
        self.assertEqual(tags['engines'], ['phaser'])
        evidence = tags['evidence'][0]
        self.assertEqual(evidence['strength'], 'detected')
        self.assertIn('/blob/'+'a'*40+'/package.json', evidence['url'])
        self.assertEqual(tags['playability'], 'unverified')
        self.assertEqual(tags['mobileCompatibility'], 'unknown')

    def test_development_only_or_transitive_packages_do_not_tag(self):
        tags = classify(repository(), files={'package.json':'{"devDependencies":{"phaser":"3"}}', 'package-lock.json':'{"phaser":"3"}'})
        self.assertFalse(tags['engines'])

    def test_unstructured_code_comments_strings_and_dead_paths_are_not_parsed(self):
        tags = classify(repository(), files={'main.js':'/*\nimport Phaser from "phaser";\n*/\nconst text = "online multiplayer"; navigator.getGamepads();'})
        self.assertFalse(tags['engines']); self.assertFalse(tags['controls']); self.assertFalse(tags['modes'])

    def test_structured_engine_signatures(self):
        tags = classify(repository(), files={'project.godot':'config_version=5\n[application]\nconfig/name="Demo"',
              'ProjectSettings/ProjectVersion.txt':'m_EditorVersion: 6000.0.0f1',
              'game.json':'{"gdVersion":{"major":4},"layouts":[]}'})
        self.assertEqual(tags['engines'], ['gdevelop','godot','unity'])

    def test_invalid_json_does_not_crash_or_invent_tags(self):
        for content in ('not json', '[]', '{"dependencies":[]}'):
            tags = classify(repository(), files={'package.json':content})
            self.assertFalse(tags['engines']); self.assertIn('invalid-json:package.json', tags['warnings'])

    def test_player_counts_and_conflicts(self):
        self.assertEqual(classify(repository(), 'Supports 2–4 players')['playerCount'], {'min':2,'max':4})
        tags = classify(repository(), 'Players: 2\nSupports 3-5 players')
        self.assertIsNone(tags['playerCount']); self.assertIn('conflicting-player-counts', tags['warnings'])
        self.assertIsNone(classify(repository(), 'Version 2.4 / 3D game')['playerCount'])
        self.assertIsNone(classify(repository(), 'Supports 8-2 players')['playerCount'])

    def test_unknown_is_explicit(self):
        tags = classify(repository())
        self.assertIn('genres', tags['unknownFields']); self.assertIn('playerCount', tags['unknownFields'])
        self.assertEqual(tags['sourceStatus'], 'pending'); self.assertEqual(tags['iframeCompatibility'], 'unknown')

    def test_contradictory_readme_downgrades_positive_topic_to_unknown(self):
        tags = classify(repository(topics=['online-multiplayer']), 'Online multiplayer is not supported.')
        self.assertEqual(tags['modes'], [])
        self.assertIn('conflicting-declaration:modes:online-multiplayer', tags['warnings'])
        self.assertFalse(any(e['field']=='modes' for e in tags['evidence']))

    def test_input_digest_changes_when_evidence_changes(self):
        first = classify(repository(), 'Genre: puzzle')
        second = classify(repository(), 'Genre: racing')
        self.assertNotEqual(first['inputDigest'], second['inputDigest'])

    def test_selected_files_are_bounded_owned_small_plain_files(self):
        blob=lambda p,**kw: dict(path=p,type='blob',mode='100644',size=50,sha='a'*40,**kw)
        tree=[blob('package.json'),blob('node_modules/game/package.json'),blob('examples/package.json'),
              dict(path='project.godot',type='blob',mode='120000',size=50),
              dict(path='big/package.json',type='blob',mode='100644',size=MAX_FILE_BYTES+1)]
        self.assertEqual([x['path'] for x in candidate_files(tree)], ['package.json'])
        many=[blob(f'game{i}/package.json') for i in range(20)]
        self.assertEqual(len(candidate_files(many)), MAX_FILES)
        self.assertEqual(candidate_files(many),candidate_files(list(reversed(many))))


class SourceClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.api_calls=[]; self.revision='a'*40; self.tree_sha='b'*40; self.pause=None
        self.files={'package.json':json.dumps({'dependencies':{'phaser':'3'}}),
                    'README.md':'# Game\nGenre: puzzle\nSingle-player.\nControls: arrow keys',
                    'index.html':'<html lang="en"><canvas></canvas>'}
    @property
    def blobs(self):
        return {hashlib.sha1(b'blob '+str(len(c.encode())).encode()+b'\0'+c.encode()).hexdigest():c for c in self.files.values()}
    def github(self,path,params=None):
        self.api_calls.append((path,params))
        if self.pause and self.pause(path): raise BudgetExpired('pause')
        if path.endswith('/commits'): return [{'sha':self.revision,'commit':{'tree':{'sha':self.tree_sha}}}]
        if '/git/trees/' in path:
            return {'tree':[{'path':p,'type':'blob','mode':'100644','size':len(c.encode()),
                    'sha':hashlib.sha1(b'blob '+str(len(c.encode())).encode()+b'\0'+c.encode()).hexdigest()} for p,c in self.files.items()], 'truncated':False}
        content=self.blobs[path.rsplit('/',1)[-1]].encode()
        return dict(encoding='base64',size=len(content),content=base64.b64encode(content).decode())


class MetadataPipelineTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:'); self.addCleanup(self.store.db.close)
        self.store.start(CONFIG,now=1000)
        self.repo=repository(language='JavaScript',topics=['puzzle-game'])
        with self.store.db: self.store.add_repository(self.repo,0)
        self.client=SourceClient()
        step_check(self.store,self.client,CONFIG)

    def test_existing_site_gains_declared_tags_without_any_extra_requests(self):
        tags=self.store.sites()[0]['metadata']
        self.assertEqual(tags['genres'],['puzzle']); self.assertEqual(tags['languages'],['JavaScript'])
        self.assertFalse(self.client.api_calls); self.assertEqual(self.store.counts()['pendingMetadataChecks'],1)

    def test_enrichment_reads_pinned_blobs_then_stops_requesting_unchanged_source(self):
        self.assertTrue(step_metadata(self.store,self.client))
        tags=self.store.sites()[0]['metadata']
        self.assertEqual(tags['engines'],['phaser']); self.assertEqual(tags['sourceRevision'],'a'*40)
        calls=len(self.client.api_calls); self.assertFalse(step_metadata(self.store,self.client))
        self.assertEqual(len(self.client.api_calls),calls)
        self.assertEqual(self.store.counts()['metadataCheckedSites'],1)

    def test_resume_does_not_refetch_successful_units(self):
        self.client.pause=lambda path:'/git/blobs/' in path
        with self.assertRaises(BudgetExpired): step_metadata(self.store,self.client)
        self.client.pause=None; step_metadata(self.store,self.client)
        self.assertEqual(sum('/commits' in p for p,_ in self.client.api_calls),1)
        self.assertEqual(sum('/git/trees/' in p for p,_ in self.client.api_calls),1)

    def test_revision_change_invalidates_source_cache(self):
        step_metadata(self.store,self.client)
        repo=dict(self.repo,pushed_at='2026-11-07T01:00:00Z')
        with self.store.db:
            self.store.add_repository(repo,0)
            self.store.observe_metadata('1',repo,'New README','2026-11-07T01:02:00Z')
        self.client.revision='c'*40
        step_metadata(self.store,self.client)
        self.assertEqual(self.store.sites()[0]['metadata']['sourceRevision'],'c'*40)
        self.assertEqual(sum('/commits' in p for p,_ in self.client.api_calls),2)

    def test_new_rules_reclassify_cached_blobs_without_network(self):
        step_metadata(self.store,self.client)
        with self.store.db:self.store.db.execute("UPDATE game_metadata SET version='old'")
        calls=len(self.client.api_calls); step_metadata(self.store,self.client)
        self.assertEqual(len(self.client.api_calls),calls)
        self.assertEqual(self.store.sites()[0]['metadata']['rulesVersion'],VERSION)

    def test_source_failure_never_removes_accepted_site(self):
        def failed(*_): raise HttpFailure(404)
        self.client.github=failed
        self.assertTrue(step_metadata(self.store,self.client)); self.assertEqual(len(self.store.sites()),1)
        self.assertEqual(self.store.sites()[0]['metadata']['sourceStatus'],'partial')
        self.assertFalse(step_metadata(self.store,self.client)) # no tight failure loop

    def test_legacy_site_without_input_is_backfilled_without_resetting_scan(self):
        with self.store.db:self.store.db.execute('DELETE FROM game_metadata')
        scan=self.store.scan
        self.assertTrue(step_metadata(self.store,self.client))
        self.assertEqual(self.store.scan,scan); self.assertEqual(self.store.sites()[0]['metadata']['modes'],['solo'])

    def test_metadata_export_filters_index_and_evidence_detail_remain_separate(self):
        step_metadata(self.store,self.client)
        with tempfile.TemporaryDirectory() as folder:
            manifest=export(self.store,CONFIG,Path(folder))
            index=json.loads((Path(folder)/manifest['indexKey']).read_text())
            details=json.loads((Path(folder)/manifest['shards'][0]['key']).read_text())
            self.assertEqual(index[0]['metadata']['engines'],['phaser'])
            self.assertNotIn('evidence',index[0]['metadata'])
            self.assertTrue(details[0]['metadata']['evidence'])
            self.assertEqual(manifest['metadataCheckedSites'],1)
            self.assertIn('updatedAt',index[0])

    def test_scheduler_prioritizes_validation_without_starving_tags_or_discovery(self):
        with patch('collect.step_check',return_value=True) as check, \
             patch('collect.step_metadata',return_value=True) as metadata, \
             patch('collect.step_partition',return_value=True) as discover, \
             patch('collect.step_repository_refresh',return_value=True) as stars, \
             patch('collect.step_attribution',return_value=True) as attribution:
            for _ in range(12): self.assertTrue(step_collection(self.store,self.client,CONFIG))
            self.assertEqual(check.call_count,8); self.assertEqual(metadata.call_count,1)
            self.assertEqual(discover.call_count,1); self.assertEqual(attribution.call_count,1)
            self.assertEqual(stars.call_count,1)

    def test_source_blob_integrity_failure_is_partial_not_a_false_engine_tag(self):
        github=self.client.github
        def corrupt(path,params=None):
            data=github(path,params)
            if '/git/blobs/' in path:data['content']=base64.b64encode(b'not original').decode();data['size']=12
            return data
        self.client.github=corrupt;step_metadata(self.store,self.client)
        tags=self.store.sites()[0]['metadata'];self.assertEqual(tags['sourceStatus'],'partial')
        self.assertFalse(tags['engines'])

    def test_failed_blob_retries_recover_without_refetching_successes(self):
        for failure_kind in ("missing", "unreadable", "legacy-checkpoint"):
            with self.subTest(failure_kind=failure_kind):
                store = Store(":memory:"); self.addCleanup(store.db.close)
                store.start(CONFIG, now=1000)
                with store.db: store.add_repository(self.repo, 0)
                client = SourceClient(); step_check(store, client, CONFIG)
                github = client.github
                package = client.files['package.json'].encode()
                sha = hashlib.sha1(b'blob ' + str(len(package)).encode() + b'\0' + package).hexdigest()
                def failed(path, params=None):
                    if path.endswith('/git/blobs/' + sha):
                        if failure_kind == "unreadable":
                            return dict(encoding='base64', size=1, content='eA==')
                        raise HttpFailure(404)
                    return github(path, params)
                client.github = failed
                step_metadata(store, client)
                self.assertEqual(store.sites()[0]['metadata']['sourceStatus'], 'partial')
                self.assertFalse(step_metadata(store, client))
                with store.db:
                    if failure_kind == "legacy-checkpoint":
                        row = store.db.execute('SELECT snapshot FROM game_metadata').fetchone()
                        snapshot = json.loads(row[0]); snapshot['done'].append('package.json')
                        store.db.execute('UPDATE game_metadata SET snapshot=?', (json.dumps(snapshot),))
                    store.db.execute("UPDATE game_metadata SET retry_after='2000-01-01T00:00:00Z'")
                client.github = github
                before = len(client.api_calls)
                self.assertTrue(step_metadata(store, client))
                calls = client.api_calls[before:]
                self.assertEqual([path for path, _ in calls], ['/repos/alice1/game/git/blobs/' + sha])
                tags = store.sites()[0]['metadata']
                self.assertEqual(tags['engines'], ['phaser'])
                self.assertEqual(tags['sourceStatus'], 'checked')
                self.assertEqual(tags['warnings'], [])
                self.assertFalse(step_metadata(store, client))

    def test_truncated_tree_is_not_reported_as_complete_inspection(self):
        github=self.client.github
        def truncated(path,params=None):
            data=github(path,params)
            if '/git/trees/' in path:data['truncated']=True
            return data
        self.client.github=truncated;step_metadata(self.store,self.client)
        self.assertEqual(self.store.sites()[0]['metadata']['sourceStatus'],'partial')

if __name__=='__main__': unittest.main()
