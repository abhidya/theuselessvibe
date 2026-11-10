# Maintaining The Useless Vibe

## Resume collection

Open **Actions → Collect and deploy → Run workflow**. Leave **refresh** unchecked. The job restores the latest SQLite artifact and continues the incomplete scan. The displayed catalog count grows only as actual reachable URLs are collected.

## Change what gets discovered

Edit `config/search.json`. Each `phrases` entry is searched separately with `baseQuery`. `discoveryQueries` adds independent topic and alternate-wording searches with the same eligibility constraints; results are deduplicated. Keep phrases explicit and specific enough to reduce matches from unrelated documentation. Appending only `discoveryQueries` to an unfinished scan preserves its existing partitions and accepted sites. Other configuration changes start a new discovery cycle.

Website phrase matching inspects fetched HTML, not canvas pixels or text produced later by JavaScript. The recorded evidence is useful during review, but is not an exhaustive classifier for games.

## Remove a reported result

1. Review the issue's source repository and URL.
2. Add the repository to `repositories` or the exact website URL to `urls` in `config/exclusions.json`.
3. Commit to `main` and follow the deployment workflow.
4. Verify the result is no longer in the directory, then close the issue with the change reference.

Repository exclusions are case-insensitive. Use the exact collected URL for a URL exclusion. Keep the exclusion in configuration so later imports cannot restore it. Do not remove someone else's source repository.

## Recovery

- **No checkpoint:** collection starts fresh. Download `catalog-checkpoint` periodically if you want a backup beyond artifact retention.
- **Rate limit reached:** the collector records progress and resumes on a later run; do not raise concurrency to bypass GitHub limits.
- **Failed export or tests:** the workflow does not publish that build. Inspect the failed step before rerunning.
- **Pages not enabled:** select **GitHub Actions** under the repository's Pages publishing settings.
- **No agent matches:** check attribution coverage. Unknown or disabled attribution is not evidence of human-only authorship.
- **Incorrect game match:** inspect the recorded Website/README signals, adjust discovery phrases, or exclude that result.

## Deployment scope

Only `site/` is published. SQLite, tokens, collector source, Actions artifacts, and issue-management controls are not placed in the Pages bundle. The public source repository and its build artifacts remain readable under GitHub's normal permissions.

## URL failures and scan priority

Unicode paths and queries are percent-encoded before sending requests. Malformed or unavailable candidate pages are recorded as rejected checks, not fatal batch errors. The collector shares bounded turns across website checks, source metadata, discovery, current-star refreshes and optional agent attribution. A new broad scan can therefore report more discovered repositories before the accepted-site count grows.

Use **GitHub Actions**, not branch publishing, as the Pages source to avoid replacing freshly generated data with the old committed snapshot. UI checks only use test fixtures and never run the collector.

Repository-scoped README errors (such as HTTP 451 or an oversized response) are recorded as unavailable for the current scan without blocking other games or deployment. Later scans reconsider them after the unsuccessful-check cache expires (at most one day). Previously verified sites are carried forward with their original link-check timestamp; a failed README read does not prove the game is unavailable. Authentication, rate-limit and service failures still pause the batch. Source metadata retries refetch failed blobs while retaining successful immutable reads.
