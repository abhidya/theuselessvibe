# GitHub Pages without collector Actions

The repository is published directly from `main` / `(root)` using GitHub Pages branch publishing. The root `index.html` redirects to `/site/`, which contains the actual application UI. `README.md` is documentation, not the home page.

`site/data/manifest.json` starts with a truthful empty catalog, so the UI loads immediately after publishing. Running a collector locally should replace the catalog, not the app files:

```bash
GITHUB_TOKEN="$(gh auth token)" python3 scripts/collect.py --config config/search.json --state .cache/catalog.sqlite --output site/data --max-seconds 900
python3 scripts/check_export.py site/data
```

Commit the generated `site/data/` files explicitly, because `.gitignore` normally excludes them:

```bash
git add -f site/data/manifest.json site/data/*/index.json site/data/*/[0-9][0-9][0-9][0-9].json
```

The `Collect and deploy` workflow is **manual-only** to respect the preference not to run lengthy collection on GitHub-hosted runners. Pushing updates to the `main` branch still invokes GitHub Pages' own normal build.

If the site still shows the README after merging, check **Settings → Pages → Build and deployment → Deploy from a branch → main → /(root)**.
