# Maintaining The Useless Vibe

## Resume collection

Open **Actions → Collect and deploy → Run workflow**. Leave **refresh** unchecked. The job restores the latest SQLite artifact and continues the incomplete scan. The displayed catalog count grows only as actual reachable URLs are collected.

## Change what gets discovered

Edit `config/search.json`. Each `phrases` entry is searched separately with `baseQuery`; results are deduplicated. Keep phrases explicit and specific enough to reduce matches from unrelated documentation. Adding a phrase changes the query set; the collector begins a new discovery cycle when configuration changes.

Website phrase matching inspects fetched HTML, not canvas pixels or text produced later by JavaScript. The Website filter is useful for narrowing results, but is not an exhaustive classifier for games.

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
