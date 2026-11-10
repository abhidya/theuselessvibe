"""Collection regressions: coverage honesty, ownership, resumption, removals."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect import (Budget, BudgetExpired, CollectorError, HttpClient, HttpFailure, ResponseTooLarge,
                     Store, candidate_urls, detect_signals, exclusion_url, is_excluded,
                     load_config, manifest_metadata, owned_pages_url, partition_query,
                     search_queries, split_range, step_attribution, step_check,
                     step_partition, step_repository_refresh, utc_iso, visible_text)


CONFIG = {
    "baseQuery": '"github.io" in:readme stars:<2 license:mit',
    "phrases": ["play online"],
    "verificationMaxAgeDays": 7,
    "attributionMaxAgeDays": 7,
}


def repository(identifier=1, **changes):
    return {
        "id": identifier, "name": "game", "full_name": f"alice{identifier}/game",
        "owner": {"login": f"alice{identifier}"}, "description": "A small game",
        "homepage": f"https://alice{identifier}.github.io/game/",
        "stargazers_count": 1, "fork": False, "private": False,
        "license": {"spdx_id": "MIT"}, "has_pages": True,
        "created_at": "2020-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-01-01T00:00:00Z", **changes,
    }


def result(items, total=None, incomplete=False):
    return {"items": items, "total_count": len(items) if total is None else total,
            "incomplete_results": incomplete}


class FakeClient:
    def __init__(self, searches=()):
        self.searches = iter(searches)
        self.requests = []
        self.readme_calls = 0
        self.page_calls = 0
        self.attribution_calls = 0

    def search(self, query, page):
        self.requests.append((query, page))
        return next(self.searches)

    def readme(self, repo):
        self.readme_calls += 1
        return f'Play online: {repo["homepage"]}'

    def page(self, url, repo):
        self.page_calls += 1
        return url, "Play online using arrow keys"

    def attribution(self, repo):
        self.attribution_calls += 1
        return {"agents": [], "agentEvidence": []}


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.start(CONFIG, now=1000)

    def tearDown(self):
        self.store.db.close()

    def seed(self, repo=None):
        with self.store.db:
            self.store.add_repository(repo or repository(), 0)
            self.store.db.execute("UPDATE partitions SET status='done'")

    def test_ranges_are_disjoint_inclusive_and_terminate(self):
        for low, high in ((0, 1), (1, 1000), (100, 101), (0, 2)):
            left, right = split_range(low, high)
            self.assertEqual(left[0], low)
            self.assertEqual(right[1], high)
            self.assertEqual(left[1] + 1, right[0])
            self.assertEqual((left[1]-left[0]+1)+(right[1]-right[0]+1), high-low+1)
        with self.assertRaises(ValueError):
            split_range(4, 4)

    def test_over_1000_splits_instead_of_truncating(self):
        client = FakeClient([result([], 38400)])
        step_partition(self.store, client, CONFIG)
        self.assertEqual(self.store.counts()["pendingPartitions"], 2)
        self.assertFalse(self.store.finish_if_complete())
        self.assertEqual(self.store.counts()["discoveredRepositories"], 0)
        self.assertIn("created:1970-01-01T00:00:00Z..", client.requests[0][0])

    def test_incomplete_response_splits_even_below_1000(self):
        step_partition(self.store, FakeClient([result([repository()], incomplete=True)]), CONFIG)
        self.assertEqual(self.store.counts()["pendingPartitions"], 2)
        self.assertEqual(self.store.counts()["discoveredRepositories"], 0)

    def test_unsplittable_response_never_claims_complete(self):
        with self.store.db:
            self.store.db.execute("UPDATE partitions SET start_second=end_second")
        step_partition(self.store, FakeClient([result([], 1001)]), CONFIG)
        metadata = manifest_metadata(self.store, CONFIG)
        self.assertEqual(metadata["unresolvedPartitions"], 1)
        self.assertFalse(metadata["complete"])
        self.assertIsNone(metadata["matchedRepositories"])

    def test_duplicate_pagination_does_not_publish_superseded_records(self):
        first = [repository(i) for i in range(1, 101)]
        second = [repository(i) for i in range(100, 150)]
        client = FakeClient([result(first, 150), result(second, 150)])
        step_partition(self.store, client, CONFIG)
        self.assertEqual(self.store.counts()["discoveredRepositories"], 0)
        step_partition(self.store, client, CONFIG)
        self.assertEqual(self.store.counts()["pendingPartitions"], 2)
        self.assertEqual(self.store.counts()["discoveredRepositories"], 0)

    def test_changing_count_and_short_page_are_not_complete(self):
        first = [repository(i) for i in range(1, 101)]
        client = FakeClient([result(first, 150), result([repository(101)], 149)])
        step_partition(self.store, client, CONFIG)
        step_partition(self.store, client, CONFIG)
        self.assertEqual(self.store.counts()["pendingPartitions"], 2)
        other = Store(":memory:")
        try:
            other.start(CONFIG, now=100)
            step_partition(other, FakeClient([result([repository()], 2)]), CONFIG)
            self.assertEqual(other.counts()["pendingPartitions"], 2)
        finally:
            other.db.close()

    def test_multiple_phrase_queries_form_a_deduplicated_union(self):
        config = dict(CONFIG, phrases=["play online", "new game"])
        self.store.start(config, refresh=True, now=1000)
        client = FakeClient([result([repository()]), result([repository()])])
        step_partition(self.store, client, config)
        step_partition(self.store, client, config)
        self.assertEqual(self.store.counts()["discoveredRepositories"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM query_matches").fetchone()[0], 2)
        self.assertEqual(manifest_metadata(self.store, config)["matchedRepositories"], 1)
        self.assertFalse(self.store.finish_if_complete())  # Still needs actual URL checks.

    def test_reachable_site_stays_unknown_when_attribution_budget_expires(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        self.assertEqual(len(self.store.sites()), 1)
        self.assertEqual(self.store.sites()[0]["attributionStatus"], "pending")
        def paused(repo):
            raise BudgetExpired("resume")
        client.attribution = paused
        with self.assertRaises(BudgetExpired):
            step_attribution(self.store, client)
        self.assertEqual(self.store.counts()["pendingAttributionChecks"], 1)
        self.assertFalse(self.store.finish_if_complete())

    def test_missing_attribution_is_explicitly_unavailable(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        def forbidden(repo):
            raise HttpFailure(403)
        client.attribution = forbidden
        step_attribution(self.store, client)
        site = self.store.sites()[0]
        self.assertEqual(site["attributionStatus"], "unavailable")
        self.assertEqual(site["agents"], [])
        self.assertEqual(self.store.counts()["unavailableAttributionChecks"], 1)

    def test_repository_readme_failure_does_not_block_later_checks(self):
        for failure in (HttpFailure(451), HttpFailure(409), ResponseTooLarge("too large")):
            with self.subTest(failure=type(failure).__name__):
                store = Store(":memory:")
                self.addCleanup(store.db.close)
                store.start(CONFIG, now=1000)
                with store.db:
                    store.add_repository(repository(1), 0)
                    store.add_repository(repository(2), 0)
                client = FakeClient()
                original = client.readme
                def readme(repo):
                    if repo["id"] == 1:
                        raise failure
                    return original(repo)
                client.readme = readme
                self.assertTrue(step_check(store, client, CONFIG))
                self.assertEqual(store.next_check()["id"], "2")
                self.assertTrue(step_check(store, client, CONFIG))
                self.assertIsNone(store.next_check())
                self.assertEqual([site["id"] for site in store.sites()], ["2"])
                # A subsequent scan retries after the negative cache expires.
                store.start(CONFIG, refresh=True, now=1001)
                with store.db:
                    store.add_repository(repository(1), 0)
                    store.db.execute("UPDATE repositories SET checked_at='2000-01-01T00:00:00Z' WHERE id='1'")
                client.readme = original
                self.assertTrue(step_check(store, client, CONFIG))
                self.assertIn("1", [site["id"] for site in store.sites()])

    def test_readme_failure_preserves_previous_site_without_claiming_recheck(self):
        self.seed(); client = FakeClient()
        step_check(self.store, client, CONFIG)
        previous = self.store.sites()[0]
        self.store.start(CONFIG, refresh=True, now=1001)
        self.seed()
        with self.store.db:
            self.store.db.execute("UPDATE repositories SET checked_at='2000-01-01T00:00:00Z'")
        def unavailable(_): raise HttpFailure(409)
        client.readme = unavailable
        self.assertTrue(step_check(self.store, client, CONFIG))
        self.assertIsNone(self.store.next_check())
        current = self.store.sites()[0]
        self.assertEqual({k: v for k, v in current.items() if k != 'starsCheckedAt'},
                         {k: v for k, v in previous.items() if k != 'starsCheckedAt'})
        self.assertEqual(self.store.counts()['currentScanEligibleSites'], 0)
        self.assertEqual(self.store.counts()['carriedForwardSites'], 1)
        self.assertEqual(client.page_calls, 1)
        # Exclusions still take precedence over preservation.
        self.store.apply_exclusions(dict(CONFIG, exclusions={'repositories':['alice1/game']}))
        self.assertEqual(self.store.sites(), [])

    def test_readme_global_failures_still_pause_without_checkpointing(self):
        self.seed()
        client = FakeClient()
        for failure in (HttpFailure(401), HttpFailure(403), HttpFailure(429),
                        HttpFailure(503), BudgetExpired("pause"), CollectorError("network unavailable")):
            def readme(_):
                raise failure
            client.readme = readme
            with self.assertRaises(type(failure)):
                step_check(self.store, client, CONFIG)
            self.assertEqual(self.store.next_check()["id"], "1")

    def test_refresh_reuses_recent_checks_and_retires_deleted_repos(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        step_attribution(self.store, client)
        self.assertTrue(self.store.finish_if_complete())
        self.store.start(CONFIG, refresh=True, now=1001)
        self.seed()
        step_check(self.store, client, CONFIG)
        self.assertEqual(client.readme_calls, 1)
        self.assertIsNone(self.store.next_attribution())
        self.store.start(CONFIG, refresh=True, now=1002)
        step_partition(self.store, FakeClient([result([])]), CONFIG)
        # Missing search results cannot distinguish growth from deletion.
        self.assertFalse(self.store.finish_if_complete())
        def missing(*_): raise HttpFailure(404)
        client.github = missing
        step_repository_refresh(self.store, client, CONFIG)
        self.assertTrue(self.store.finish_if_complete())
        self.assertEqual(self.store.sites(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0], 0)

    def test_finished_scan_automatically_starts_next_collection(self):
        client = FakeClient([result([])])
        step_partition(self.store, client, CONFIG)
        self.assertTrue(self.store.finish_if_complete())
        old_scan = self.store.scan
        self.store.start(CONFIG, now=1001)
        self.assertNotEqual(self.store.scan, old_scan)
        self.assertIsNone(self.store.get("finishedAt"))
        self.assertEqual(self.store.counts()["pendingPartitions"], 1)

    def test_config_changes_start_new_scan_but_export_only_does_not(self):
        old_scan = self.store.scan
        updated = dict(CONFIG, phrases=["WASD", "new game"])
        with self.assertRaises(CollectorError):
            self.store.start(updated, export_only=True)
        self.assertEqual(self.store.scan, old_scan)
        self.store.start(updated, now=1001)
        self.assertNotEqual(self.store.scan, old_scan)
        self.assertEqual(self.store.counts()["pendingPartitions"], 2)
        self.assertEqual(manifest_metadata(self.store, updated)["terms"], ["WASD", "new game"])

    def test_refresh_keeps_previous_catalog_until_replacement_finishes(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        step_attribution(self.store, client)
        self.store.finish_if_complete()
        self.store.start(CONFIG, now=1001)
        self.assertEqual(len(self.store.sites()), 1)
        counts = self.store.counts()
        self.assertEqual(counts["eligibleSites"], 1)
        self.assertEqual(counts["currentScanEligibleSites"], 0)
        self.assertEqual(counts["carriedForwardSites"], 1)
        # A graduated game no longer matches discovery, but is refreshed by ID.
        step_partition(self.store, FakeClient([result([])]), CONFIG)
        client.github = lambda *_: repository(stargazers_count=42)
        step_repository_refresh(self.store, client, CONFIG)
        step_check(self.store, client, CONFIG)
        metadata = manifest_metadata(self.store, CONFIG)
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["eligibleSites"], 1)
        self.assertEqual(metadata["carriedForwardSites"], 0)
        self.assertEqual(self.store.sites()[0]["stars"], 42)
        self.assertEqual(metadata["discoveredRepositories"], 0)

    def test_changed_stars_retain_a_previously_admitted_site(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        self.store.start(CONFIG, refresh=True, now=1001)
        with self.store.db:
            self.store.add_repository(repository(stargazers_count=2), 0)
        self.assertEqual(self.store.sites()[0]["stars"], 2)
        step_check(self.store, client, CONFIG)
        self.assertEqual(client.readme_calls, 1)
        self.assertEqual(self.store.sites()[0]["stars"], 2)

    def test_exclusions_survive_resume_and_refresh(self):
        self.seed()
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        config = dict(CONFIG, exclusions={"repositories": ["ALICE1/GAME"], "urls": []})
        self.store.start(config)  # An exclusion does not require a new crawl.
        self.store.apply_exclusions(config)
        self.assertEqual(self.store.sites(), [])
        self.assertEqual(manifest_metadata(self.store, config)["eligibleSites"], 0)
        self.store.start(config, refresh=True, now=1001)
        self.seed()
        step_check(self.store, client, config)
        self.assertEqual(self.store.sites(), [])
        self.assertEqual(client.readme_calls, 1)

    def test_url_removal_covers_cached_query_and_fragment_variants(self):
        self.seed(repository(homepage="https://alice1.github.io/game/?mode=easy#play"))
        client = FakeClient()
        step_check(self.store, client, CONFIG)
        config = dict(CONFIG, exclusions={"urls": ["https://ALICE1.github.io/game"]})
        self.store.apply_exclusions(config)
        self.assertEqual(self.store.sites(), [])
        self.assertTrue(is_excluded(repository(), "https://alice1.github.io/game/?v=2", config))

    def test_unknown_or_unreachable_pages_do_not_become_catalog_sites(self):
        self.seed()
        client = FakeClient()
        client.page = lambda url, repo: None
        step_check(self.store, client, CONFIG)
        self.assertEqual(self.store.sites(), [])
        self.assertTrue(self.store.finish_if_complete())


class URLAndSignalTests(unittest.TestCase):
    def test_only_owned_repo_pages_are_allowed(self):
        repo = repository()
        self.assertEqual(owned_pages_url("https://ALICE1.github.io/game/#start", repo), "https://alice1.github.io/game/")
        for bad in (
            "http://alice1.github.io/game/", "https://evil.github.io/game/",
            "https://alice1.github.io/another-game/", "https://alice1.github.io/game-copy/",
            "https://alice1.github.io.evil.test/game/", "https://alice1.github.io@127.0.0.1/game/",
            "https://alice1.github.io:444/game/", "https://alice1.github.io/game/../other/",
            "https://alice1.github.io/game/%2e%2e/other/", "https://alice1.github.io/game/%252e%252e/other/",
            "https://alice1.github.io/game%2fother/", "https://alice1.github.io\\@evil.test/game/",
            "javascript:alert(1)", "file:///game/", "https://alice1.github.io/game/\n",
        ):
            # Trailing whitespace is harmless around a pasted URL; embedded controls are not.
            if bad.endswith("\n"):
                bad = "https://alice1.github.io/ga\nme/"
            self.assertIsNone(owned_pages_url(bad, repo), bad)

    def test_user_site_does_not_claim_links_to_other_project_sites(self):
        repo = repository(name="alice1.github.io", full_name="alice1/alice1.github.io")
        self.assertEqual(owned_pages_url("https://alice1.github.io/", repo), "https://alice1.github.io/")
        self.assertIsNone(owned_pages_url("https://alice1.github.io/another-project/", repo))

    def test_markdown_links_are_extracted_without_guessing_pages_url(self):
        repo = repository(homepage=None)
        text = "[Play](https://alice1.github.io/game/) [library](https://someone.github.io/tool/)"
        self.assertEqual(candidate_urls(repo, text), ["https://alice1.github.io/game/"])
        self.assertEqual(candidate_urls(repo, "No demo is published"), [])

    def test_phrase_evidence_normalizes_whitespace_and_ignores_scripts(self):
        page = visible_text("<head><title>WASD</title></head><script>new game</script><style>play online</style><p>Arrow\n keys</p>")
        self.assertEqual(page, "Arrow keys")
        signals = detect_signals(["play online", "arrow keys", "WASD", "new game"], "PLAY\n online; wasd123", page)
        self.assertEqual(signals, [{"term": "play online", "source": "readme"}, {"term": "arrow keys", "source": "page"}])

    def test_config_loads_separate_maintainer_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "search.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            (path.parent / "exclusions.json").write_text('{"repositories":["alice1/game"],"urls":[]}', encoding="utf-8")
            config = load_config(path)
            self.assertTrue(is_excluded(repository(), None, config))
            self.assertEqual(search_queries(config), ['"github.io" in:readme stars:<2 license:mit "play online"'])


class ResumptionTest(unittest.TestCase):
    def test_partial_page_resume_survives_database_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            store = Store(path)
            store.start(CONFIG, now=1000)
            scan = store.scan
            step_partition(store, FakeClient([result([repository(i) for i in range(1, 101)], 150)]), CONFIG)
            store.db.close()
            restored = Store(path)
            try:
                restored.start(CONFIG)
                self.assertEqual(restored.scan, scan)
                step_partition(restored, FakeClient([result([repository(i) for i in range(101, 151)], 150)]), CONFIG)
                self.assertEqual(restored.counts()["discoveredRepositories"], 150)
                self.assertEqual(restored.counts()["pendingPartitions"], 0)
                self.assertEqual(restored.counts()["pendingRepositoryChecks"], 150)
            finally:
                restored.db.close()


class Response:
    def __init__(self, status, headers, body=b""):
        self.code, self.headers, self.stream = status, headers, io.BytesIO(body)

    def read(self, length=-1):
        return self.stream.read(length)

    read1 = read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def open(self, request, timeout):
        self.calls.append(request)
        return next(self.responses)


class HTTPBoundaryTests(unittest.TestCase):
    def test_pages_redirect_cannot_escape_owned_path(self):
        client = HttpClient(Budget(60), timeout=1)
        client.opener = Opener([Response(302, {"Location": "http://127.0.0.1/secrets"})])
        self.assertIsNone(client.page("https://alice1.github.io/game/", repository()))
        self.assertEqual(len(client.opener.calls), 1)

    def test_github_token_is_not_forwarded_to_cross_host_redirect(self):
        client = HttpClient(Budget(60), token="test-only-token", timeout=1)
        client.opener = Opener([Response(302, {"Location": "https://external.example/api"})])
        with self.assertRaises(HttpFailure):
            client.github("/repos/alice1/game")
        self.assertEqual(len(client.opener.calls), 1)

    def test_secondary_rate_limit_without_retry_header_pauses_safely(self):
        client = HttpClient(Budget(5), timeout=1)
        client.opener = Opener([Response(403, {}, b'{"message":"You have exceeded a secondary rate limit"}')])
        with self.assertRaises(BudgetExpired):
            client.github("/repos/alice1/game/issues")

    def test_non_html_and_404_are_not_marked_reachable(self):
        for response in (Response(200, {"Content-Type": "application/json"}, b'{}'), Response(404, {}, b'missing')):
            client = HttpClient(Budget(60), timeout=1)
            client.opener = Opener([response])
            self.assertIsNone(client.page("https://alice1.github.io/game/", repository()))


if __name__ == "__main__":
    unittest.main()
