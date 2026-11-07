# The Useless Vibe

A random door into small GitHub Pages projects. A giant **Surprise me** button opens a discovered website, while the directory lets you browse recent updates, search phrases, inspect Codex or Claude attribution, star the source repository, and report a result.

The application is static HTML, CSS, and JavaScript. A Python collector uses SQLite and exports JSON. **No R2 bucket, Worker, server database, paid service, npm dependencies, or browser token is required.**

## What it collects

`config/search.json` defines independent GitHub repository searches. Each phrase is combined with:

```text
"github.io" in:readme stars:<2 license:mit
```

Initial phrases are `play online`, `play in browser`, `play in your browser`, `start game`, `new game`, `arrow keys`, and `WASD`. The collector combines the results by repository ID. GitHub's normal fork exclusion is reinforced by checking repository metadata. Independent copies that GitHub does not classify as forks can still appear.

The collector extracts actual links from the repository homepage and README. It accepts HTTPS GitHub Pages URLs owned by that repository's owner and matching its project path; unrelated documentation links are excluded. It checks for a successful HTML response and records matching phrases in the README and page text. It does not execute or mirror other people's games.

**These are discovery signals, not a guarantee that every result is a game.** Canvas-only or JavaScript-rendered text may be missed. A successful HTML response does not prove that every game mechanic works. The site displays the count of collected reachable websites, never an invented 38,400-site catalog. The earlier 38,400 figure came from a different, broader repository search. No 1,000-commit threshold is applied by the current phrase-based search.

## Generate the files locally

Requirements: Python 3.12+ and Node.js 22+ for tests. There are no application packages to install.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs

# Uses a GitHub token already present in the environment, when available.
# With GitHub CLI installed and authenticated:
GITHUB_TOKEN="$(gh auth token)" python3 scripts/collect.py \
  --config config/search.json \
  --state .cache/catalog.sqlite \
  --output site/data \
  --max-seconds 900

python3 scripts/check_export.py site/data
python3 -m http.server 8000 --directory site
```

Open `http://localhost:8000`. The collector stops at its work budget, checkpoints its progress, and publishes the verified portion of the catalog. Run the same command again to continue. Use `--refresh` to restart discovery while retaining recently checked URLs. Treat the token as a secret: never put it in source files, Pages files, or logs.

## Create the repository and deploy

For a fresh downloaded project directory, with Git and an authenticated [GitHub CLI](https://cli.github.com/) installed:

```bash
bash scripts/publish.sh abhidya/theuselessvibe
```

The script creates a **new public repository**, pushes the source, enables GitHub Pages with the Actions publishing source, and starts **Collect and deploy**. It refuses to overwrite an existing repository. If your local Git identity is not configured, set it before running the script. If the repository is created but enabling Pages fails, finish the setup under **Settings → Pages → Build and deployment → GitHub Actions**, then run **Actions → Collect and deploy → Run workflow**.

For a repository that you created yourself, push these source files to `main`, select the same Pages publishing source, then run the workflow. The workflow uses its automatic `GITHUB_TOKEN`; no personal access token or Cloudflare secret needs to be configured.

The workflow:

1. Runs the Python and JavaScript tests.
2. Downloads the latest SQLite checkpoint artifact, if one exists.
3. Collects a batch for up to 15 minutes, respecting API limits and saving progress.
4. Exports and checks the static catalog.
5. Saves SQLite as a checkpoint and publishes `site/` to GitHub Pages.

It runs on `main` pushes, manual dispatch, and a six-hour schedule. A complete initial backfill may span many runs. The catalog shows partial coverage while work remains. GitHub can delay scheduled runs and disables scheduled workflows in inactive public repositories; you can restart the workflow manually.

## Where each file lives

| Content | Location | Purpose |
| --- | --- | --- |
| App, scripts, search configuration, removals | Git repository | Human-reviewable source and settings |
| Collector state and evidence | `.cache/catalog.sqlite` | Deduplication, scan progress, URL checks, agent attribution |
| SQLite between hosted runs | `catalog-checkpoint` Actions artifact | Resume without re-fetching everything |
| Public catalog | `site/data/manifest.json`, compact index, 256-site shards | Read by the browser; deployed as a Pages artifact |
| Reports | GitHub issues in `abhidya/theuselessvibe` | Maintainer review and removal requests |

Generated catalog files and SQLite are excluded from Git to avoid committing a new binary database and dataset on every refresh. Checkpoint artifacts expire after 90 days. If the checkpoint has expired, collection starts again; retain a local download if you need a longer-lived backup. **Artifacts in this public repository are not private storage.** The database contains public project metadata, and never contains tokens.

## Search and filter behavior

- **Recently updated:** results are ordered by the repository timestamp recorded by the collector, newest first.
- **Text search:** matches names, repository names, compact descriptions, and detected phrases.
- **Phrase chips:** any selected phrase may match (OR).
- **Match in:** restricts phrase evidence to the website's fetched HTML, its README, or either.
- **Codex / Claude:** requires observed agent attribution in the sampled recent activity.
- Different filter types combine (AND). Random chooses uniformly from the same filtered set as the directory.

The browser fetches a compact filter index only when needed and loads full records in small shards. It does not download a SQLite engine or the collector database.

## Agent attribution

The collector examines the latest 30 default-branch commits and pull requests found among the latest 100 repository issue records. It records links for:

- PRs created through the **ChatGPT Codex Connector** GitHub app (`performed_via_github_app`).
- Exact recognized Codex or Claude bot authors.
- Explicit `Co-Authored-By` trailers with the recognized agent and service address.
- Explicit, recognized “Generated with” attribution footers.

An agent name mentioned in ordinary prose, a `CLAUDE.md` file, a topic, or a review comment alone does not count. The filter indicates observed attribution, not proof that the whole project was generated by that agent. Attribution can be hidden or customized, and the limited scan can miss older credits. Pending or unavailable scans remain unknown and stay visible under All.

## Star, report, and remove

**Star on GitHub** opens the source repository. The visitor signs in and chooses GitHub's Star action there; this site does not obtain account permissions or claim to star anything automatically.

**Report / request removal** opens a prefilled issue form on `abhidya/theuselessvibe`. The issue includes the website URL and source repository. Reports do not automatically delete entries.

To remove a result, edit `config/exclusions.json`:

```json
{
  "repositories": ["owner/project"],
  "urls": ["https://someone.github.io/unwanted/"]
}
```

Commit the change. The next build excludes it from the directory and Random, and future scans preserve the exclusion. Removing a result does not modify the original repository or website.

## Limits and licensing

GitHub search returns at most 1,000 results per query. The collector divides searches into disjoint creation-time ranges, records incomplete responses and unresolved partitions, and never calls a truncated scan complete. A repository can change while a scan runs, so this is a refreshed search index, not a transactional snapshot of all GitHub.

MIT is the repository's declared license; individual art, music, dependencies, and names may have different terms. This directory links to projects and does not grant additional rights or mirror their content. Website availability and star counts can change after checking.

## References

- [GitHub repository search](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
- [GitHub search API limits](https://docs.github.com/en/rest/search)
- [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [Actions artifact retention](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
- [Claude Code attribution settings](https://code.claude.com/docs/en/settings-reference#attribution)
- [Codex GitHub integration and reviews](https://developers.openai.com/codex/integrations/github/)

The directory's own code is [MIT licensed](LICENSE). The interaction concept is inspired by [The Useless Web](https://theuselessweb.com/); this is a separate project with its own design and catalog.
