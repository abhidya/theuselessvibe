# Partial catalog export (2026-11-07)

This data comes from the `catalog-checkpoint` SQLite artifact saved during the **canceled** GitHub-hosted collection run `42329930270`. The ZIP was downloaded into the assistant's working environment. The local export passed two structural tests (IDs, sort ordering, MIT and 0–1 star metadata, URL format, index parity, honest scan status). No new GitHub Actions run was started for this export.

This snapshot has **83 reachable GitHub Pages URLs** from 883 discovered repositories. Of those, **128 had been checked**, and **755 had not**. Only one query (`play online`) had begun scanning; the other phrase searches are not represented. The source repo's README or site HTML contained matching terms, but page interactivity has **not** been verified. The Codex and Claude filters currently have no completed attribution checks; zero matches means **unknown**, not human-authored.

The export intentionally omits repository descriptions, which are not necessary for the UI's navigation, and keeps metadata and explicit text signals from the checkpoint. To resume the SQLite collector locally, restore the original ZIP as `.cache/catalog.sqlite` and run `python3 scripts/collect.py --state .cache/catalog.sqlite --output site/data --config config/search.json --max-seconds 900`. Do **not** run the scheduled GitHub workflow; the root Pages entrypoint and manual-only collector workflow are in PR #1.

Rechecks should update or remove sites that no longer satisfy reachability, star, or licensing criteria.
