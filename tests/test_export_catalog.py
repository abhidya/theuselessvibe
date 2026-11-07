import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import export_catalog as exporter


def metadata(**extra):
    return {
        'query': '"github.io" in:readme stars:<2 license:mit',
        'searchUrl': 'https://github.com/search?q=game&type=repositories&s=updated&o=desc',
        'generatedAt': '2026-11-06T00:00:00Z',
        'matchedRepositories': None, 'scannedRepositories': 38400,
        'complete': False, 'pendingAttributionChecks': 1, **extra,
    }


def site(index, **extra):
    return {
        'id': str(index), 'repo': f'alice/game-{index}', 'name': f'Game {index}',
        'description': 'A puzzle ' * 40,
        'url': f'https://alice.github.io/game-{index}/',
        'repoUrl': f'https://github.com/alice/game-{index}',
        'stars': 0, 'license': 'MIT', 'updatedAt': '2026-11-06T00:00:00Z',
        'createdAt': '2026-11-01T00:00:00Z', 'checkedAt': '2026-11-06T00:00:00Z',
        'agents': [], 'agentEvidence': [], 'signals': [],
        'attributionStatus': 'pending', 'attributionCheckedAt': None,
        'attributionScope': 'Latest 30 commits and recent pull requests', **extra,
    }


class ExportCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def read(self, key):
        return json.loads((self.directory / key).read_text())

    def test_shards_order_index_and_attribution_coverage(self):
        sites = [site(i) for i in range(257)]
        sites[256].update({
            'updatedAt': '2026-11-06T01:00:00Z', 'agents': ['codex'],
            'signals': [
                {'term': 'WASD', 'source': 'page'},
                {'term': 'play online', 'source': 'readme'},
            ],
        })
        manifest = exporter.export_catalog(sites, metadata(phrases=['start game']), self.directory)
        self.assertEqual([entry['count'] for entry in manifest['shards']], [256, 1])
        self.assertEqual(manifest['eligibleSites'], 257)
        self.assertIsNone(manifest['matchedRepositories'])
        self.assertEqual(manifest['agentCounts'], {'codex': 1, 'claude': 0})
        self.assertEqual(manifest['pendingAttributionChecks'], 1)
        self.assertEqual(manifest['terms'], ['play online', 'start game', 'wasd'])
        index = self.read(manifest['indexKey'])
        self.assertEqual(index[0]['id'], '256')
        self.assertEqual(index[0]['index'], 0)
        self.assertEqual(index[0]['pageTerms'], ['wasd'])
        self.assertEqual(index[0]['readmeTerms'], ['play online'])
        self.assertLessEqual(len(index[0]['description']), 160)
        full_site = self.read(manifest['shards'][0]['key'])[0]
        self.assertEqual(full_site['attributionStatus'], 'pending')
        self.assertEqual(full_site['description'], sites[256]['description'])
        self.assertEqual(manifest, self.read('manifest.json'))

    def test_empty_export_is_valid_and_deterministic(self):
        first = exporter.export_catalog([], metadata(), self.directory)
        second = exporter.export_catalog([], metadata(), self.directory)
        self.assertEqual(first, second)
        self.assertEqual(first['eligibleSites'], 0)
        self.assertEqual(first['shards'], [])
        self.assertEqual(self.read(first['indexKey']), [])

    def test_scan_lifecycle_metadata_is_preserved(self):
        lifecycle = {
            'carriedForwardSites': 0, 'currentScanEligibleSites': 0,
            'discoveredRepositories': 12, 'pendingRepositoryChecks': 12,
            'pendingPartitions': 3, 'scanStartedAt': '2026-11-06T00:00:00Z',
            'scanFinishedAt': None,
            'reportedMatchesByQuery': [{'query': 'game', 'count': 38, 'incomplete': False}],
            'attributionScope': 'Latest 30 commits and recent pull requests',
        }
        manifest = exporter.export_catalog([], metadata(**lifecycle), self.directory)
        for key, value in lifecycle.items():
            self.assertEqual(manifest[key], value, key)

    def test_failed_new_generation_does_not_replace_working_manifest(self):
        original = exporter.export_catalog([site(0)], metadata(), self.directory)
        write = exporter._atomic_write

        def fail_index(path, content):
            if path.name == 'index.json':
                raise OSError('Simulated disk failure')
            write(path, content)

        with patch.object(exporter, '_atomic_write', side_effect=fail_index):
            with self.assertRaises(OSError):
                exporter.export_catalog([site(1)], metadata(), self.directory)
        self.assertEqual(self.read('manifest.json'), original)
        self.assertTrue((self.directory / original['shards'][0]['key']).is_file())

    def test_only_current_and_previous_generated_directories_are_retained(self):
        unrelated = self.directory / 'manual-assets'
        unrelated.mkdir()
        first = exporter.export_catalog([site(0)], metadata(), self.directory)
        second = exporter.export_catalog([site(1)], metadata(), self.directory)
        third = exporter.export_catalog([site(2)], metadata(), self.directory)
        self.assertFalse((self.directory / first['generation']).exists())
        self.assertTrue((self.directory / second['generation']).is_dir())
        self.assertTrue((self.directory / third['generation']).is_dir())
        self.assertTrue(unrelated.is_dir())

    def test_duplicate_ids_are_rejected_before_manifest_changes(self):
        original = exporter.export_catalog([site(0)], metadata(), self.directory)
        with self.assertRaises(ValueError):
            exporter.export_catalog([site(1), site(1)], metadata(), self.directory)
        self.assertEqual(self.read('manifest.json'), original)


if __name__ == '__main__':
    unittest.main()
