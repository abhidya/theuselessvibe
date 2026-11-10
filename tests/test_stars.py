"""Graduating games retain admission; popularity is observed, never invented."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect import (BudgetExpired, CollectorError, HttpFailure, ResponseTooLarge, Store,
                     step_check, step_collection, step_repository_refresh)
from test_collector import CONFIG, FakeClient, repository


class RepositoryClient(FakeClient):
    def __init__(self, response=None, error=None):
        super().__init__()
        self.response = response if response is not None else repository(stargazers_count=42)
        self.error = error
        self.api_calls = []

    def github(self, path):
        self.api_calls.append(path)
        if self.error:
            raise self.error
        return self.response


class CurrentStarsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.store.start(CONFIG, now=1000)
        self.client = RepositoryClient()

    def admit(self, identifier=1):
        with self.store.db:
            self.store.add_repository(repository(identifier), 0)
        step_check(self.store, self.client, CONFIG)
        self.assertIn(str(identifier), [site["id"] for site in self.store.sites()])

    def new_scan(self):
        self.store.start(CONFIG, refresh=True, now=1001)
        with self.store.db:
            self.store.db.execute("UPDATE partitions SET status='done'")

    def test_independent_refresh_updates_cached_page_and_can_decrease_to_zero(self):
        self.admit()
        checked_at = self.store.sites()[0]["checkedAt"]
        self.new_scan()
        self.assertEqual(self.store.counts()["pendingStarRefreshes"], 1)
        self.assertTrue(step_repository_refresh(self.store, self.client, CONFIG))
        self.assertEqual(self.client.api_calls, ["/repositories/1"])
        self.assertEqual(self.store.counts()["discoveredRepositories"], 0)
        self.assertEqual(self.store.sites()[0]["stars"], 42)
        self.assertIsNotNone(self.store.sites()[0]["starsCheckedAt"])
        step_check(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites()[0]["checkedAt"], checked_at)
        self.assertEqual(self.client.page_calls, 1)
        self.assertFalse(step_repository_refresh(self.store, self.client, CONFIG))
        self.new_scan()
        self.client.response = repository(stargazers_count=0)
        step_repository_refresh(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites()[0]["stars"], 0)

    def test_repository_local_failures_keep_last_observation_and_back_off(self):
        self.admit()
        original = self.store.sites()[0]
        failures = (HttpFailure(400), HttpFailure(409), HttpFailure(422), ResponseTooLarge("oversized"))
        for failure in failures:
            with self.subTest(failure=str(failure)):
                self.new_scan()
                with self.store.db:
                    self.store.db.execute("UPDATE repositories SET repository_retry_after=NULL")
                self.client.error = failure
                self.assertTrue(step_repository_refresh(self.store, self.client, CONFIG))
                current = self.store.sites()[0]
                self.assertEqual(current["stars"], original["stars"])
                self.assertEqual(current["starsCheckedAt"], original["starsCheckedAt"])
                self.assertEqual(current["checkedAt"], original["checkedAt"])
                self.assertEqual(self.store.counts()["unavailableStarRefreshes"], 1)
                self.assertEqual(self.store.counts()["pendingStarRefreshes"], 0)
                self.assertFalse(step_repository_refresh(self.store, self.client, CONFIG))
        with self.store.db:
            self.store.db.execute("UPDATE repositories SET repository_retry_after='2000-01-01T00:00:00Z'")
        self.client.error = None
        step_repository_refresh(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites()[0]["stars"], 42)
        self.assertEqual(self.store.counts()["unavailableStarRefreshes"], 0)

    def test_global_failures_pause_without_deferring_or_changing_last_observation(self):
        self.admit(); self.new_scan()
        original = dict(self.store.db.execute("SELECT * FROM repositories").fetchone())
        for failure in (HttpFailure(401), HttpFailure(403), HttpFailure(429), HttpFailure(503), CollectorError("offline")):
            with self.subTest(failure=str(failure)):
                self.client.error = failure
                with self.assertRaises(type(failure)):
                    step_repository_refresh(self.store, self.client, CONFIG)
                self.assertEqual(dict(self.store.db.execute("SELECT * FROM repositories").fetchone()), original)
                self.assertEqual(self.store.counts()["pendingStarRefreshes"], 1)
                self.assertEqual(self.store.counts()["unavailableStarRefreshes"], 0)

    def test_budget_interruption_leaves_refresh_due_and_rotates_work(self):
        self.admit(); self.new_scan()
        with self.store.db:
            self.store.put("workTurn", 10)  # The dedicated star lane.
        self.client.error = BudgetExpired("pause")
        with self.assertRaises(BudgetExpired):
            step_collection(self.store, self.client, CONFIG)
        self.assertEqual(self.store.get("workTurn"), 11)
        self.assertEqual(self.store.next_repository_refresh()["id"], "1")
        with patch("collect.step_attribution", return_value=True) as attribution:
            self.assertTrue(step_collection(self.store, self.client, CONFIG))
        attribution.assert_called_once()
        self.assertEqual(self.client.api_calls, ["/repositories/1"])

    def test_refresh_removes_deleted_private_forked_or_non_mit_repositories(self):
        cases = [(None, HttpFailure(404)), (None, HttpFailure(410)), (None, HttpFailure(451)),
                 ({"private": True}, None), ({"fork": True}, None),
                 ({"license": {"spdx_id": "GPL-3.0"}}, None), ({"license": None}, None)]
        for change, error in cases:
            with self.subTest(change=change, error=str(error)):
                self.new_scan(); self.admit(); self.new_scan()
                self.client.error = error
                self.client.response = repository(stargazers_count=100, **(change or {}))
                step_repository_refresh(self.store, self.client, CONFIG)
                self.assertEqual(self.store.sites(), [])
                row = self.store.db.execute("SELECT admitted_at FROM repositories").fetchone()
                self.assertIsNone(row["admitted_at"])
                self.assertIsNone(self.store.next_repository_refresh())

    def test_malformed_or_wrong_id_responses_do_not_change_last_observation(self):
        self.admit(); self.new_scan()
        original = self.store.sites()[0]
        responses = [repository(2), repository(stargazers_count=-1), repository(stargazers_count=True),
                     repository(stargazers_count=None), {"id": 1}, None, []]
        for response in responses:
            with self.subTest(response=response):
                with self.store.db:
                    self.store.db.execute("UPDATE repositories SET repository_retry_after=NULL")
                self.client.response = response
                self.assertTrue(step_repository_refresh(self.store, self.client, CONFIG))
                self.assertEqual(self.store.sites()[0], original)

    def test_periodic_refresh_runs_during_long_scan_without_rediscovery(self):
        self.admit()
        scan = self.store.scan
        self.assertIsNone(self.store.next_repository_refresh())
        with self.store.db:
            self.store.db.execute("UPDATE repositories SET repository_checked_at='2000-01-01T00:00:00Z'")
        self.assertTrue(step_repository_refresh(self.store, self.client, CONFIG))
        self.assertEqual(self.store.scan, scan)
        self.assertFalse(step_repository_refresh(self.store, self.client, CONFIG))
        self.assertEqual(len(self.client.api_calls), 1)

    def test_periodic_refresh_does_not_prevent_discovery_scan_completing(self):
        self.admit()
        with self.store.db:
            self.store.db.execute("UPDATE partitions SET status='done'")
            self.store.db.execute("UPDATE repositories SET attribution_status='checked', repository_checked_at='2000-01-01T00:00:00Z'")
        self.assertEqual(self.store.counts()['pendingStarRefreshes'], 1)
        self.assertEqual(self.store.counts()['pendingScanStarRefreshes'], 0)
        self.assertTrue(self.store.finish_if_complete())
        old_scan = self.store.scan
        self.store.start(CONFIG, now=1002)
        self.assertNotEqual(self.store.scan, old_scan)
        self.assertEqual(self.store.counts()['pendingScanStarRefreshes'], 1)
        self.assertEqual(self.store.get('throughSecond'), 1002)

    def test_temporarily_unreachable_graduate_can_return_after_search_finishes(self):
        self.admit(); self.new_scan()
        step_repository_refresh(self.store, self.client, CONFIG)
        with self.store.db:
            self.store.db.execute("UPDATE repositories SET checked_at='2000-01-01T00:00:00Z'")
        self.client.page = lambda *_: None
        step_check(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites(), [])
        self.assertIsNotNone(self.store.db.execute("SELECT admitted_at FROM repositories").fetchone()[0])
        self.assertTrue(self.store.finish_if_complete())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0], 1)
        self.new_scan()
        step_repository_refresh(self.store, self.client, CONFIG)
        with self.store.db:
            self.store.db.execute("UPDATE repositories SET checked_at='2000-01-01T00:00:00Z'")
        self.client.page = lambda url, _: (url, "Play online")
        step_check(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites()[0]["stars"], 42)

    def test_exclusions_revoke_admission_and_skip_refresh_network_calls(self):
        for exclusions in ({"repositories": ["alice1/game"]}, {"urls": ["https://alice1.github.io/game/"]}):
            with self.subTest(exclusions=exclusions):
                self.new_scan(); self.admit(); self.new_scan()
                config = dict(CONFIG, exclusions=exclusions)
                self.assertTrue(step_repository_refresh(self.store, self.client, config))
                self.assertEqual(self.store.sites(), [])
                self.assertIsNone(self.store.next_repository_refresh())
        self.assertEqual(self.client.api_calls, [])

    def test_transfer_cannot_leave_old_owners_page_in_the_catalog(self):
        self.admit(); self.new_scan()
        self.client.response = repository(owner={"login": "bob"}, full_name="bob/game",
                                          homepage="https://bob.github.io/game/", stargazers_count=40)
        step_repository_refresh(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites(), [])
        step_check(self.store, self.client, CONFIG)
        self.assertEqual(self.store.sites()[0]["url"], "https://bob.github.io/game/")
        self.assertEqual(self.store.sites()[0]["repo"], "bob/game")

    def test_old_buffered_search_observation_cannot_overwrite_newer_refresh(self):
        self.admit(); self.new_scan()
        step_repository_refresh(self.store, self.client, CONFIG)
        with self.store.db:
            self.store.add_repository(repository(), 0, observed_at="2000-01-01T00:00:00Z")
        self.assertEqual(self.store.sites()[0]["stars"], 42)
        self.assertEqual(self.store.counts()["discoveredRepositories"], 1)

    def test_same_second_search_checkpoint_cannot_replace_direct_refresh(self):
        self.admit(); self.new_scan()
        step_repository_refresh(self.store, self.client, CONFIG)
        timestamp = self.store.sites()[0]["starsCheckedAt"]
        with self.store.db:
            self.store.add_repository(repository(), 0, observed_at=timestamp)
        self.assertEqual(self.store.sites()[0]["stars"], 42)


class StarCheckpointTests(unittest.TestCase):
    def test_migration_preserves_admission_but_does_not_invent_fresh_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite"
            store = Store(path); store.start(CONFIG, now=1000)
            with store.db: store.add_repository(repository(), 0)
            step_check(store, FakeClient(), CONFIG)
            store.db.close()
            # Recreate the schema from the pre-star version's checkpoint.
            db = sqlite3.connect(path)
            for column in ("admitted_at", "stars_checked_at", "repository_checked_at",
                           "repository_checked_scan", "repository_retry_after"):
                db.execute(f"ALTER TABLE repositories DROP COLUMN {column}")
            db.commit(); db.close()
            migrated = Store(path)
            try:
                migrated.start(CONFIG)
                self.assertIsNotNone(migrated.next_repository_refresh())
                self.assertEqual(migrated.sites()[0]["stars"], 1)
                self.assertIsNone(migrated.sites()[0]["starsCheckedAt"])
                step_repository_refresh(migrated, RepositoryClient(), CONFIG)
                self.assertEqual(migrated.sites()[0]["stars"], 42)
                self.assertIsNotNone(migrated.sites()[0]["starsCheckedAt"])
            finally: migrated.db.close()

    def test_refresh_queue_and_failures_resume_without_repeating_earlier_work(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite"
            store = Store(path); store.start(CONFIG, now=1000)
            for identifier in (1, 2):
                with store.db: store.add_repository(repository(identifier), 0)
                step_check(store, FakeClient(), CONFIG)
            store.start(CONFIG, refresh=True, now=1001)
            client = RepositoryClient(error=HttpFailure(409))
            step_repository_refresh(store, client, CONFIG)
            self.assertEqual(client.api_calls, ["/repositories/1"])
            store.db.close()
            resumed = Store(path)
            try:
                resumed.start(CONFIG)
                self.assertEqual(resumed.next_repository_refresh()["id"], "2")
                client = RepositoryClient(repository(2, stargazers_count=200))
                step_repository_refresh(resumed, client, CONFIG)
                self.assertEqual(client.api_calls, ["/repositories/2"])
                self.assertIsNone(resumed.next_repository_refresh())
                self.assertEqual(resumed.counts()["unavailableStarRefreshes"], 1)
            finally: resumed.db.close()


if __name__ == "__main__":
    unittest.main()
