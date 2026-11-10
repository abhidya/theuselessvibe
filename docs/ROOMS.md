# Shared discovery rooms

Open **Room** in the collection header. Enter a code such as `PIZZA`, or follow a copied room link. No account, host, room service, WebSocket, database, or new collection job is involved.

## User behavior

- Codes are case-insensitive and normalize to uppercase. The same code and catalog edition produce the same complete order. Personal discovery filters are deliberately not applied to rooms.
- First visits start at the first game. **Previous** and **Next** move through the order and wrap with modulo. Your controls do not move anyone else's device or synchronize gameplay.
- **History** reopens visited positions. Up to 100 visits and the current position are saved locally for each of up to 20 room/edition pairs. Browsers that block storage can still play, without persistence. Returning to a game reloads its iframe; its internal gameplay state is not saved by this site.
- **Copy room link** includes the normalized code and catalog edition, not the sender's position or rotation preference. Returning visitors resume their own saved position. New visitors start at the beginning.
- **Auto-rotate every minute** starts off on every join and refresh. When explicitly enabled, the device's Unix time supplies the index: `floor(Date.now() / 60000) % count`. Joining halfway through a minute selects the current minute's game. Regaining visibility recomputes the current slot rather than replaying missed ticks.
- Switching auto-rotate off keeps the displayed game. Previous, Next, History, and native browser navigation return that device to manual browsing. Accurate device clocks are required; there is no shared server clock or host-controlled advancement.

## Ordering and catalog changes

The browser loads the existing compact catalog index, assigns each game a SHA-256 rank from `['room-v1', normalizedCode, gameId]`, then sorts those ranks with a deterministic ID tie-break. This is a repeatable permutation, not a random call on each click. Sorting does not depend on locale or the directory's star ranking. The positive modulo operation wraps manual indices in both directions. Details are loaded lazily using the existing catalog adapter; game content is not downloaded upfront or mirrored.

A room is scoped to a catalog edition. Sharing pins the existing edition identifier. The adapter checks the edition and selected game ID again before displaying details. If a new deployment retires an edition, the page stops rather than silently selecting a different game and offers **Join the current catalog edition**. Share the resulting new link with the group. Old editions are not archived indefinitely by this feature. Entering only a bare code uses the current edition; people on different editions must rejoin together to have the same positions.

## Interface and boundaries

`site/room.html` is a separate room player linked from the collection header. The existing solo discovery flow and collector are unchanged. The room page uses pinned Bootstrap 5.3.8 CSS from jsDelivr, with the official subresource-integrity hash, plus a small layout stylesheet. Native dialogs provide the room settings, history and secondary game actions. The three navigation buttons occupy a separate bottom row; no permanent help banner covers the game.

Room data and links are public, not private authenticated rooms. The code selects an ordering; it is not an access credential. External games stay sandboxed, without top-level navigation permissions. Same-origin games additionally omit `allow-same-origin`. Star on GitHub is visible in the player header and opens the repository in a new tab; sign in if needed, then click GitHub’s Star button. Open directly and Report/removal remain in More. Nothing is starred or reported automatically.

## Verification

Run the existing test commands:

```sh
node --test tests/*.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
```

The room tests cover deterministic ordering (including an independently checked SHA-256 vector), multiple device sessions, wrapping, manual defaults, clock alignment and catch-up, stopping rotation, persistence, blocked storage, history limits, asynchronous races, sharing and catalog edition mismatches. The full Node and Python suites passed locally.

Browser execution was attempted but blocked with `ERR_BLOCKED_BY_ADMINISTRATOR` on the local test page. No browser/device visual sign-off or live deployment verification is claimed. Bootstrap CDN loading and actual third-party game compatibility remain browser QA items.

Before merging, check 320px/390px phone widths and desktop: all three navigation buttons fit, menus open and close with focus restoration, two devices on a shared link see the same sequence, and auto-rotation remains off until selected. Only enable it deliberately when testing minute-boundary behavior.

References: https://getbootstrap.com/docs/5.3/getting-started/introduction/ and https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/digest
