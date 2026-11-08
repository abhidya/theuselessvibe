# Deterministic game discovery and metadata

## Status and scope

This change implements deterministic metadata extraction, source inspection, static filtering, and balanced collection. It is based on `main` commit `6742b7cf6497bb7fba1ec69bb4cb3efab2f50cbe`.

**This is not the requested Stitch redesign.** The supplied Stitch ZIP was recognized by the attachment service but did not materialize into readable bytes in the execution environment. Existing Bootstrap layouts and player/room behavior are preserved. The only UI additions are a collapsed metadata filter section, at most two genre/mode badges per card, and a collapsed evidence view. Do not describe this branch as the completed combined design PR.

No model calls, new hosted service, credentials, external database, new runtime dependencies, repository builds, or execution of collected source code are introduced. No new real games have been claimed as validated by the local tests.

## Discovery

The original 18 independent searches are retained, in the same order. Thirteen additional repository queries are appended, for a total of 31: Phaser, Kaplay, Unity WebGL, GDevelop, Construct 3, puzzle, platformer, arcade, tower defense, racing, roguelike, Godot/Pages README, and multiplayer/Pages README. All appended queries add `stars:<2 license:mit fork:false`. The existing metadata eligibility check still rejects forks, private repositories, non-MIT licenses, and repositories with two or more stars. The collector still requires an owned, reachable GitHub Pages HTML page and game evidence; discovery is not acceptance or proof of playability.

Use repository-search syntax here. In particular, do **not** put modern web code-search qualifiers such as `content:` or `symbol:` in REST repository searches. Global code search is not added to the crawler; bounded source inspection is used after a site is accepted. The REST code-search endpoint uses the legacy syntax and has its own limits. Existing creation-time partitioning, incomplete-results checks, and repository-ID deduplication remain in place.

A read-only smoke query for `topic:phaser stars:<2 license:mit fork:false` was accepted by the GitHub connector and returned candidate repositories. This does not measure precision/recall, verify the candidates' Pages sites, or establish query completeness.

## Evidence, not certification

Every positive metadata tag has a rule identifier, source URL/path, excerpt, and strength. Source blobs have a pinned commit revision; README text already read during link checking is identified as an observation, not misrepresented as a pinned source revision. The metadata includes a deterministic input digest and rules version. `observedAt` is acquisition metadata, not a classification feature.

| Field | Rule | Missing/ambiguous evidence |
|---|---|---|
| Genre | Exact repository-topic allowlist; explicit `Genre:` / `Genres:` README field | Empty array / Unknown |
| Mode | Exact topics or current README feature phrases | Unknown; bare multiplayer remains `multiplayer-unspecified` |
| Player count | Explicit `Players: N` or `Supports N–M players` (1–64) | `null`; conflicting ranges stay unknown |
| Programming language | GitHub primary `language`, retained from search metadata | Unknown, not inferred from an engine |
| Page language | Declared `html lang` in a selected source HTML file | Unknown; not proof of complete translation |
| Engine | Direct runtime dependencies in parsed package JSON; Godot, Unity, or GDevelop project markers | Unknown |
| Rendering library | Direct PixiJS/Three.js/Babylon.js dependencies | Separate from engine and game genre |
| Controls | Current author-declared keyboard/mouse/touch/gamepad phrases | Unknown; no device compatibility claim |
| Repository dates | Existing creation/update/link-check timestamps | Kept distinct from a game's release date |
| Mobile/iframe/playability | No new runtime verifier in this change | Always unknown/unverified |

`declared` means a repository/topic/document statement, not an independent test. `detected` means a supported structured source signature was observed, not that the engine is active in the deployed build. Networking packages do not imply multiplayer. A canvas or viewport tag does not imply a game or mobile compatibility. Development-only/transitive dependencies are not engine evidence. Comments, code fences, quotations, future sections, unchecked tasks, and negative declarations are not treated as positive README evidence; contradictory mode/control statements remove a positive tag. These are intentionally conservative English-language rules, not an exhaustive natural-language classifier.

Unstructured JavaScript/TypeScript substring matching is intentionally **not implemented**: comments, strings, examples, and dead code would create misleading tags without a proper parser. Likewise, thumbnails, runtime mobile checks, actual multiplayer sessions, and automatic genre inference are not fabricated. Unknown is a valid output.

## Collection and state

`game_metadata` is an additive SQLite table. It holds cheap observed README evidence, source/cache keys, a bounded source snapshot, classifier output/version, inspection state, and retry time. It does not reset discovery cursors or replace existing accepted sites.

Each successful website check immediately computes low-cost topic/README/language tags without another request. Source inspection is a separate resumable stage. Old accepted records without metadata are backfilled. A source pass makes one request for the default-branch commit, one for its recursive tree, and at most six small immutable blobs. Files are selected deterministically, symlinks/submodules/vendor/test/doc trees are excluded, each file is capped at 64 KiB, and total cached source per repository is capped at 128 KiB. Observed README input is separately capped at 64 KiB. Blob bytes are checked against their Git SHA before classification. Selected-file limits, truncation and unsupported formats are not proof that the whole repository was inspected.

Successful network units are checkpointed before the next request. Budget/rate-limit pauses propagate to the existing resumable runner. Changed pushed/default-branch inputs invalidate the snapshot. A rules-version change reapplies rules to cached immutable blobs rather than refetching unchanged source. Other inspection failures retain the accepted website, mark metadata partial and defer retries for a day instead of looping. Existing code already handles GitHub's rate-limit headers and budgets; this feature does not raise concurrency or attempt to bypass limits.

A persisted ten-turn schedule gives eight turns to pending website checks, one to source metadata, and one to discovery. Empty lanes yield to useful pending work. Optional Codex/Claude attribution stays last. This prevents both the large candidate backlog and new discovery from monopolizing every batch.

The manifest reports `pendingMetadataChecks`, `metadataCheckedSites`, and `metadataPartialSites` separately. `checked` means the bounded source pass completed, not that every field is known or that the game was played. The existing `complete` flag retains its collection meaning; it is not a blanket metadata/playability guarantee.

## Export and UI

Full detail shards include evidence. The compact index includes normalized metadata values and the repository date fields, but not excerpts or raw source. Detail shards are bounded by both 256 records and 3.5 MB to stay below the browser's 4 MiB response cap. Legacy records with no metadata remain visible and are filterable as Unknown.

The collapsed filter panel exposes genre, mode, primary programming language, declared page language, engine, controls, declared player count, and repository-updated-after. Filter types intersect. Browse and Play random use exactly the same predicate; the shared room sequence remains unfiltered and unchanged. Option counts are snapshot-wide rather than dynamically cross-filtered. No Codex/Claude controls are reintroduced. Card details explain strengths and provide source links; all imported text is rendered as text, not injected HTML.

## Tests and remaining work

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
```

Local result: 82 Python + 54 Node tests passed (136 total). Coverage includes negation/future false positives, exact tags, player-count conflicts, unknowns, source SHA checks, bounded selection, resumable cache writes, rule/revision invalidation, legacy checkpoint backfill, no loss of accepted games on inspection failure, balanced work scheduling, export/shard size, evidence URLs, client filters and identical Browse/Random selection.

`tests/ui_smoke.py` now exercises metadata filtering and preserves the original failure if screenshot capture also crashes. These browser scenarios have **not** been run in this execution environment, and no physical-phone or live-game results are claimed. A live collector run with the new rules is also outstanding. The dataset of classification assertions is synthetic regression data, not a real-world accuracy benchmark. A manually labeled evaluation set is still needed before making precision/recall claims.

Before release: apply the actual Stitch HTML/images, run Chromium and WebKit checks at phone/landscape/desktop sizes, execute a limited live collector batch, inspect tag/evidence quality on real games, then review the PR. Keep shared-room auto-rotate off by default.

## Primary references

- GitHub repository query syntax: https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories
- REST code search uses legacy syntax: https://docs.github.com/en/search-github/searching-on-github/searching-code
- Search result caps and pagination: https://docs.github.com/en/rest/search
- Rate limits (including the Actions token and separate search limits): https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- REST request/cache guidance: https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
- Recursive trees and truncation: https://docs.github.com/en/rest/git/trees
- Phaser package identifier: https://docs.phaser.io/phaser/getting-started/installation
- Godot project settings: https://docs.godotengine.org/en/stable/tutorials/editor/project_settings.html
- Unity's documented project version marker: https://github.com/Unity-Technologies/skills/blob/main/skills/unity-pipeline/SKILL.md
- GDevelop project version marker discussion: https://github.com/4ian/GDevelop/discussions/6324
