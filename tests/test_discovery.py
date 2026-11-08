"""Regression tests for the production Unicode crash and broader discovery."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import (Store, Budget, HttpClient, BudgetExpired, CollectorError,
                     candidate_urls, request_url, owned_pages_url, game_evidence,
                     load_config, search_queries, step_check, step_collection,
                     is_excluded, export)
from test_collector import CONFIG, FakeClient, repository, Response, Opener, result


class URLRegressionTests(unittest.TestCase):
    def test_unicode_request_and_redirect_are_ascii_without_double_encoding(self):
        client = HttpClient(Budget(60), timeout=1)
        client.opener = Opener([
            Response(302, {'Location': '/game/niveau…?label=café%20test'}),
            Response(200, {'Content-Type': 'text/html'}, b'<p>Play now</p>'),
        ])
        url, text = client.page('https://alice1.github.io/game/日本?x=…', repository())
        self.assertEqual(text, 'Play now')
        self.assertIn('niveau%E2%80%A6?label=caf%C3%A9%20test', url)
        for req in client.opener.calls:
            req.full_url.encode('ascii')
            self.assertNotIn('%2520', req.full_url)

    def test_bad_candidate_does_not_block_the_next_url(self):
        store = Store(':memory:'); self.addCleanup(store.db.close)
        store.start(CONFIG, now=1000)
        repo = repository(homepage='https://alice1.github.io/game/…')
        with store.db: store.add_repository(repo, 0)
        client = HttpClient(Budget(60), timeout=1)
        client.readme = lambda repo: 'Play here: https://alice1.github.io/game/'
        client.opener = Opener([Response(404, {}, b'missing'), Response(200, {'Content-Type': 'text/html'}, b'<p>play here</p>')])
        self.assertTrue(step_check(store, client, CONFIG))
        self.assertEqual(store.sites()[0]['url'], 'https://alice1.github.io/game/')
        self.assertIsNone(store.next_check())

    def test_malformed_unicode_is_rejected_but_valid_unicode_is_not(self):
        self.assertIsNone(owned_pages_url('https://alice1.github.io/game/\ud800', repository()))
        self.assertIsNotNone(owned_pages_url('https://alice1.github.io/game/日本', repository()))
        for path in ['/game/%2e%2e/other', '/game/%252e%252e/other', '/game%2Fother/']:
            self.assertIsNone(owned_pages_url('https://alice1.github.io' + path, repository()))
        with self.assertRaises(ValueError): request_url('https://alice1.github.io/ga\nme')

    def test_encoded_and_unicode_removals_have_the_same_effect(self):
        config = dict(CONFIG, exclusions={'urls': ['https://alice1.github.io/game/café']})
        self.assertTrue(is_excluded(repository(), 'https://alice1.github.io/game/caf%C3%A9?q=1', config))

    def test_page_errors_still_respect_budget_and_origin_boundaries(self):
        client = HttpClient(Budget(60), timeout=1)
        client.opener = Opener([Response(302, {'Location': 'https://other.github.io/game/'})])
        self.assertIsNone(client.page('https://alice1.github.io/game/', repository()))
        self.assertEqual(len(client.opener.calls), 1)
        with patch.object(client, '_request', side_effect=BudgetExpired('pause')):
            with self.assertRaises(BudgetExpired): client.page('https://alice1.github.io/game/', repository())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:'); self.addCleanup(self.store.db.close)
        self.store.start(CONFIG, now=1000)

    def test_all_new_sources_keep_eligibility_and_include_topics_without_readme_url(self):
        config = load_config(Path(__file__).resolve().parents[1] / 'config/search.json')
        queries = search_queries(config)
        self.assertEqual(len(queries), len(set(queries)))
        for query in queries:
            self.assertIn('stars:<2', query); self.assertIn('license:mit', query)
        topics = [q for q in queries if q.startswith('topic:')]
        self.assertGreaterEqual(len(topics), 6)
        self.assertTrue(all('github.io' not in q and 'fork:false' in q for q in topics))

    def test_append_sources_preserves_existing_partition_and_unchecked_candidates(self):
        from collect import step_partition
        step_partition(self.store, FakeClient([result([repository()])]), CONFIG)
        scan = self.store.scan
        partition = dict(self.store.db.execute('SELECT * FROM partitions').fetchone())
        config = dict(CONFIG, discoveryQueries=['topic:browser-game'])
        self.store.start(config)
        self.assertEqual(self.store.scan, scan)
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM partitions WHERE id=?', (partition['id'],)).fetchone()), partition)
        self.assertEqual(self.store.counts()['discoveredRepositories'], 1)
        self.assertEqual(self.store.counts()['pendingPartitions'], 1)
        # A second resume must not append duplicate partitions.
        self.store.start(config)
        self.assertEqual(self.store.counts()['pendingPartitions'], 1)

    def test_topic_or_playable_at_can_qualify_without_legacy_phrases(self):
        for repo, readme in [(repository(topics=['browser-game'], description='', name='test'), ''),
                             (repository(), 'Multiplayer Pacman. Playable at: https://alice1.github.io/game/')]:
            store = Store(':memory:')
            try:
                store.start(CONFIG, now=1000)
                with store.db: store.add_repository(repo, 0)
                client = FakeClient(); client.readme = lambda _: readme
                client.page = lambda url, _: (url, 'Welcome')
                self.assertTrue(step_check(store, client, CONFIG))
                self.assertEqual(len(store.sites()), 1)
                self.assertTrue(store.sites()[0]['gameEvidence'])
            finally: store.db.close()

    def test_default_pages_url_is_only_a_candidate_and_requires_reachability(self):
        repo = repository(homepage=None)
        self.assertEqual(candidate_urls(repo, '', include_default=True), ['https://alice1.github.io/game/'])
        self.assertEqual(candidate_urls(repository(homepage=None, has_pages=False), '', include_default=True), [])
        with self.store.db: self.store.add_repository(repo, 0)
        client = FakeClient(); client.readme = lambda _: 'A browser game'; client.page = lambda *args: None
        step_check(self.store, client, CONFIG)
        self.assertEqual(self.store.sites(), [])

    def test_reachable_html_alone_does_not_qualify(self):
        repo = repository(name='manual', full_name='alice1/manual', description='Documentation', homepage='https://alice1.github.io/manual/')
        with self.store.db: self.store.add_repository(repo, 0)
        client = FakeClient(); client.readme = lambda _: 'Documentation'
        client.page = lambda url, _: (url, 'Welcome to our documentation')
        step_check(self.store, client, CONFIG)
        self.assertEqual(self.store.sites(), [])

    def test_discovery_is_not_blocked_by_optional_attribution(self):
        with patch('collect.step_partition', return_value=True) as discover, \
             patch('collect.step_check') as check, patch('collect.step_attribution') as attribution:
            self.assertTrue(step_collection(self.store, FakeClient(), CONFIG))
            discover.assert_called_once(); check.assert_not_called(); attribution.assert_not_called()

    def test_new_candidates_still_reject_forks_wrong_license_or_two_stars(self):
        for change in ({'fork': True}, {'license': {'spdx_id': 'GPL-3.0'}}, {'stargazers_count': 2}):
            store = Store(':memory:')
            try:
                store.start(CONFIG, now=1000)
                with store.db: store.add_repository(repository(**change), 0)
                step_check(store, FakeClient(), CONFIG)
                self.assertEqual(store.sites(), [])
            finally: store.db.close()

    def test_export_retains_evidence_and_partial_coverage(self):
        with self.store.db: self.store.add_repository(repository(), 0)
        step_check(self.store, FakeClient(), CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            metadata = export(self.store, CONFIG, Path(directory))
            self.assertFalse(metadata['complete']); self.assertEqual(metadata['eligibleSites'], 1)
            sites = json.loads((Path(directory) / metadata['shards'][0]['key']).read_text())
            self.assertTrue(sites[0]['gameEvidence'])

    def test_invalid_discovery_config_is_rejected(self):
        for value in ('topic:game', [None], ['x\nlicense:other'], ['a'] * 25):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'search.json'
                path.write_text(json.dumps(dict(CONFIG, discoveryQueries=value)))
                with self.assertRaises(CollectorError): load_config(path)

if __name__ == '__main__': unittest.main()
