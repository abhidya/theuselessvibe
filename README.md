# The Useless Vibe

A static GitHub Pages directory for overlooked browser games. **Play random → Previous / History / Next** is the main flow. Search the directory or join a shared room with friends. A room code gives everyone the same order; auto-rotation is optional and **off by default**.

## Interface

The solo collection and room player use the same pinned **Bootstrap 5.3.8** stylesheet and player geometry. Games fill the space between a compact header and a three-button bottom navigation bar. Star, Report and Open directly live in the **More** dialog; help text does not cover the game. Search matches project names, descriptions and collected signals. Development-agent attribution is not presented as a game category.

Solo history stays on the device (up to 100 visits) and survives refresh. Previous and Next revisit the trail; Next discovers a game when at its end. Browsing back reloads third-party games: their internal progress cannot be saved here. Some games prohibit iframe embedding or need controls your device does not support; More always offers a direct link.

See [room behavior and catalog editions](docs/ROOMS.md). Rooms are not multiplayer networking: they share the ordering, not gameplay or other people's button presses.

## Discovery and collection

`config/search.json` keeps the original seven phrase queries and adds independent searches for game topics, the broad `game "github.io" in:readme` query, and alternate play-link wording. Every new source includes **0–1 stars, MIT, no forks**, reinforced by metadata checks. Copies that GitHub does not classify as forks may still appear.

The resumable Python collector uses SQLite to:

1. Partition every search by creation time to avoid GitHub's 1,000-result search window, then deduplicate by repository ID.
2. Find owned HTTPS GitHub Pages links in homepage/README metadata. Game evidence can also justify probing the conventional owner/project Pages URL; a constructed URL is only a candidate until it returns HTML.
3. Record exact phrases and other game evidence (topics, descriptive terms, alternative play prompts). A short README no longer has to contain one of the original seven phrases.
4. Check optional recent commit/PR attribution **after** discovery and website checks, then export a compact JSON index and detail shards for the browser.

Unicode URL paths and query strings are percent-encoded before requests, including redirects. Invalid candidates fail individually; redirects still must remain inside the repository's allowed Pages origin/path. Exclusions apply to encoded and Unicode equivalents.

Adding only new discovery queries to an unfinished scan appends partitions without discarding its progress or good sites. Previously rejected rows are rechecked under the broader rules. Other search configuration changes, explicit `--refresh`, or the next completed-scan cycle start a replacement scan; prior accepted sites remain available during it. No checkpoint reset is needed for this PR.

**Reachable HTML plus game evidence is not proof of playability.** Counts report the collected snapshot, not an invented 30,000-game inventory. Repository license metadata does not automatically license every image, soundtrack or name.

## Local development

Python 3.12+ and Node.js 22+. No application package installation or backend is needed.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs

# Uses the token in your environment; never commit or publish it.
GITHUB_TOKEN="$(gh auth token)" python3 scripts/collect.py \
  --config config/search.json --state .cache/catalog.sqlite \
  --output site/data --max-seconds 600
python3 scripts/check_export.py site/data
python3 -m http.server 8000 --directory site
```

SQLite is the resumable collection state, not a browser dependency. Static JSON is deployed with the site. No R2, hosted database, account system, or client-side GitHub token is required.

## Deployment and removals

Use **Settings → Pages → GitHub Actions** for `Collect and deploy`. The workflow restores the latest SQLite checkpoint, collects a bounded batch, saves progress, validates the export and publishes `site/`. It runs on main pushes, manual dispatch and a six-hour schedule. Collection is incremental across runs; completeness is explicitly recorded. Do not also use branch-based Pages publishing to replace the generated catalog with the old committed snapshot.

The public repository's checkpoint artifacts contain public metadata, not secrets. They expire after 90 days; download a backup when longer retention matters. Generated JSON and SQLite are otherwise ignored by Git.

Reports open a prefilled issue in this repository; they do not automatically delete a site. Add reviewed removals to `config/exclusions.json` and deploy. This affects the directory, not the source project. See [operations](docs/OPERATIONS.md).

## UI verification

`UI browser checks` runs the actual Bootstrap styles and JavaScript in **Chromium and WebKit**, with 320px, 390px, landscape and desktop viewports. The test uses synthetic fixture games, never production catalog entries. It checks button visibility/hit targets, iframe boundaries, native dialogs, history/refresh/Back/Forward, search, disabled storage and independent shared-room navigation. It uploads screenshots and a JSON report. A passing fixture test does not guarantee that every external game embeds or works on physical iOS hardware.

```bash
python3 -m pip install playwright==1.56.0
python3 -m playwright install --with-deps chromium webkit
python3 tests/ui_smoke.py
```

## References

- [Bootstrap documentation](https://getbootstrap.com/docs/5.3/getting-started/introduction/)
- [GitHub repository search](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
- [Python URL quoting](https://docs.python.org/3/library/urllib.parse.html#url-quoting)
- [Playwright CI](https://playwright.dev/python/docs/ci)

The directory is [MIT licensed](LICENSE), inspired by [The Useless Web](https://theuselessweb.com/). Projects belong to their respective creators.
