import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from attribution import detect_attribution


class AttributionTests(unittest.TestCase):
    def commit(self, message, **extra):
        return {"html_url": "https://github.com/example/game/commit/abcdef123", "commit": {"message": message}, **extra}

    def pr(self, **extra):
        return {"html_url": "https://github.com/example/game/pull/7", "pull_request": {}, **extra}

    def test_claude_trailer_identifies_model_variants(self):
        result = detect_attribution([self.commit("Add a level\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>")], [])
        self.assertEqual(result["agents"], ["claude"])
        self.assertEqual(result["agentEvidence"][0]["kind"], "commit-trailer")

    def test_connector_app_counts_for_human_pr_author(self):
        result = detect_attribution([], [self.pr(user={"login": "human", "type": "User"}, performed_via_github_app={"slug": "chatgpt-codex-connector"})])
        self.assertEqual(result["agents"], ["codex"])
        self.assertEqual(result["agentEvidence"][0]["kind"], "pr-app")

    def test_plain_mentions_review_request_and_regular_issue_do_not_count(self):
        commits = [self.commit("Fix Codex API integration and Claude settings")]
        pulls = [self.pr(body="@codex review\nUses Claude Code for docs"), {"html_url": "https://github.com/example/game/issues/1", "performed_via_github_app": {"slug": "chatgpt-codex-connector"}}]
        self.assertEqual(detect_attribution(commits, pulls)["agents"], [])

    def test_documented_generated_footer(self):
        result = detect_attribution([], [self.pr(body="## Changes\nAdd levels\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")])
        self.assertEqual(result["agents"], ["claude"])

    def test_no_lookalike_evidence_urls_or_bot_names(self):
        commit = self.commit("Co-Authored-By: Claude <noreply@anthropic.com>")
        commit["html_url"] = "https://github.com.attacker.example/example/game/commit/abcdef123"
        fake_bot = self.commit("ordinary", author={"login": "claude[bot]", "type": "User"})
        self.assertEqual(detect_attribution([commit, fake_bot], [])["agents"], [])

    def test_evidence_is_bounded_and_deduplicated(self):
        record = self.commit("Co-Authored-By: Claude <noreply@anthropic.com>")
        self.assertEqual(len(detect_attribution([record] * 100, [])["agentEvidence"]), 1)


if __name__ == "__main__":
    unittest.main()
