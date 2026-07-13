---
title: 'Convert ATP Tour rankings to Patchright with browser-exclusive queueing'
type: 'behavior-change-design'
created: '2026-07-13'
status: 'approved-design'
review_loop_iteration: 0
baseline_commit: 'e7483fa07b2868a657d409ce4667c0c1c1800f6c'
context: []
---

<frozen-after-approval reason="human-owned intent - do not modify unless human renegotiates">

## Intent

**Problem:** ATP Tour rankings currently use `curl_cffi` requests through `ScraperClient`. ATP's Cloudflare challenge intermittently blocks the assigned `plainproxies.com` datacenter proxy before ranking discovery or during player-profile enrichment. Failed run `efc41d2a` received HTTP 403 on all eight top-100 attempts and produced zero rows; other same-day runs produced only 6 or 166 rows while a healthy run produced 4,993 rows.

**Approach:** Make ATP a Patchright-only browser scraper. Use one `BrowserClient` for ranking discovery and up to `Scraper.worker_count` independent browser clients for concurrent player enrichment. Every browser uses the scraper's currently assigned proxy and ATP host allowlist. Add `uses_browser=True` to ATP's registry specification so the existing queue and immediate-start guards give the run system-wide browser exclusivity.

## Boundaries And Constraints

**Always:** Keep the assigned datacenter proxy unchanged; use Patchright for both rankings HTML and player hero JSON; issue hero JSON calls through BrowserClient's in-page `fetch()` path; give each worker thread its own browser; wrap the complete browser phase in one `allow_async_unsafe()` scope; honor the configured retry budget; close every browser; preserve ranking schema, date-window semantics, rank-type selection, bio caching, and deduplication.

**Ask First:** Changing proxy assignment/type, adding curl fallback, changing the global queue policy, using persistent browser profiles, changing ATP CSV columns, or raising browser concurrency above `Scraper.worker_count`.

**Never:** Do not share a `BrowserClient` across threads; do not duplicate browser-exclusivity logic outside the existing registry-driven guards; do not use `context.request` for protected JSON; do not log raw proxy addresses or credentials; do not create a migration for `uses_browser`; do not silently report complete success after final browser/ranking/profile failures.

</frozen-after-approval>

## Investigation Evidence

- `atptour.py` currently constructs `ScraperClient` for discovery and each enrichment worker. Its module documentation already states that curl cannot solve a hard ATP Cloudflare JavaScript challenge.
- Failed run `efc41d2a` made four singles and four doubles top-100 requests. Every response was HTTP 403, so discovery stopped before parsing or enrichment.
- Same-day run `04ebfb8e` had 63 ranking 403s, 788 hero 403s, and only 6 output rows but was marked successful. Run `d47cfd71` had 62 ranking 403s, 166 hero 403s, and 166 rows. Healthy run `008ce9e1` had 32 ranking HTTP 200 responses, 3,192 hero HTTP 200 responses, and 4,993 rows.
- ATP is assigned active proxy `plainproxies.com`, kind `datacenter`. It is the only configured active proxy pool and must remain assigned.
- `BrowserClient` already supports Patchright persistent contexts, assigned proxy translation, host/public-IP guards, challenge detection, browser relaunch, and protected JSON via in-page `fetch()`.
- `ScraperSpec.uses_browser` already drives `_capacity_snapshot()`, `_dispatch_next()`, `_create_guarded_run()`, UI blocker messages, scheduler/web/batch queue admission, and `scrape_now` exclusivity. ATP currently omits this flag.

## Architecture

### Registry And Queueing

- Set `uses_browser=True` on `registry.SPECS["atptour"]`.
- Do not add another lock or queue condition. The existing registry-driven behavior becomes authoritative for ATP:
  - A queued ATP run starts only when no other run is live.
  - While ATP is live, no request-based or browser-based run starts.
  - A head-of-queue ATP run that cannot start blocks later request jobs, preventing browser-job starvation.
  - `scrape_now atptour` refuses to start when any other run is live, and other `scrape_now` jobs refuse while ATP is live.
- No database migration is needed because `uses_browser` is runtime registry metadata, not a model field.

### Browser Factory

ATP owns a small factory that creates `BrowserClient` instances with:

- `proxy=scraper.proxy` so the existing datacenter pool is preserved.
- `allowed_hosts=("www.atptour.com",)`.
- `headless` and `channel` from existing `SCRAPER_BROWSER_*` settings.
- An ephemeral profile per client. Each browser persists only for its phase or worker chunk, then deletes its temporary profile on close.
- `manage_async_unsafe=False`; the runner owns one outer `allow_async_unsafe()` scope.
- `announce=True` for the discovery browser and quiet enrichment clients, with one safe run-level pool message instead of duplicate launch messages.
- No proxy-session placeholder manipulation. Independent browser connections use the configured pool as supplied.

### Ranking Discovery

1. Compute snapshot dates and rank types exactly as today.
2. Open one discovery browser inside `allow_async_unsafe()`.
3. For each singles/doubles rank-range URL, navigate with `BrowserClient.get_selector()` and parse the existing ATP table selectors.
4. If navigation returns `None`, relaunch the browser and retry the same URL. Total navigation attempts equal the inherited `browser.api_tries` retry budget.
5. Track whether any ranking page exhausts retries or whether a top-100 page renders without expected rows. Preserve the existing early stop for a failed top-100 table so the scraper does not hammer the remaining ranges.
6. Preserve current player IDs, ranks, points, snapshot dates, range ordering, and per-table deduplication.

### Player Enrichment

1. Set `progress_total` to the number of discovered player-week rows, as today.
2. Split players into at most `Scraper.worker_count` chunks.
3. Each worker thread creates and owns one independent `BrowserClient` for its entire chunk.
4. Before profile requests, each worker navigates to an ATP ranking page using the same bounded relaunch helper. This primes the protected ATP origin and clearance state.
5. Reuse the existing `_enrich_one()` mapping and shared locked bio cache. `BrowserClient.get_json()` supplies hero JSON through its in-page `fetch()` implementation.
6. Increment progress exactly once per assigned player, including when a worker browser cannot launch or prime.
7. Close each worker browser in all success/failure paths. Never move a browser instance between threads.

## Failure And Status Semantics

- Browser import/launch failure before discovery: record a clear browser-unavailable error and return `FAILED`, zero rows.
- Ranking navigation exhaustion or missing top-100 rows: stop that table, mark the run incomplete, and continue another requested rank type if possible.
- Worker launch/prime failure: mark every player in that chunk failed, advance their progress, and allow other chunks to continue.
- Final profile failure after BrowserClient retries: skip that player, advance progress, and mark the run incomplete.
- No discovered or enriched rows: `FAILED`.
- At least one row plus any final ranking, worker, or profile failure: `PARTIAL`.
- Rows with no final failures: `SUCCESS`.
- A transient challenge that succeeds within the bounded relaunch/retry budget does not by itself make the run partial.
- There is no curl fallback. Patchright failure remains explicit and honest.

## I/O And Edge Cases

| Scenario | Input / State | Expected Behavior |
|----------|---------------|-------------------|
| Healthy current-week run | Singles and doubles, assigned datacenter proxy | Browser parses all ranges, enriches players through in-page JSON, returns success |
| First page challenged once | Browser page returns challenge, relaunch succeeds | Same URL retried; run continues without curl |
| Top-100 challenge persists | All browser navigation attempts fail | That table stops; failed or partial depends on other table output |
| One later range fails | Earlier ranges produced players | Keep discovered players and mark run partial |
| One worker cannot launch | Other worker browsers succeed | Failed chunk progress is completed; successful rows remain; status partial |
| One player hero fails | Other profiles succeed | Skip one row, progress completes, status partial |
| Duplicate player in singles/doubles | Same player ID appears in both tables | Bio cache may be reused; each ranking row remains distinct by rank type |
| Multiple weekly snapshots | Date range contains several Mondays | Existing one-row-per-player-per-week output is preserved |
| Request job already running | ATP reaches queue head | ATP remains queued and blocks later jobs until the server is idle |
| ATP running | Another request/browser job is queued or started via CLI | Queued job remains queued; guarded CLI start is rejected |

## Test Design

### ATP Runner Tests

Create `accounts/tests/test_atptour.py` with fake browser clients; unit tests must not require a real Chrome process.

- Prove the browser factory receives the assigned proxy, ATP host allowlist, existing headless/channel settings, ephemeral profile, and `manage_async_unsafe=False`.
- Prove a challenged ranking navigation relaunches and retries only to the configured budget.
- Prove discovery keeps current URL/date/rank parsing and stops a persistently failed top-100 table.
- Prove each enrichment worker creates one browser and no browser instance is used by multiple thread IDs.
- Prove workers prime ATP before calling protected hero JSON and close browsers after completion.
- Prove profile caching, player mapping, per-player progress, and rank-type/date output remain unchanged.
- Prove complete, partial, and failed status outcomes from final-failure state rather than merely `row_count > 0`.
- Prove no production path constructs `ScraperClient` or invokes a curl fallback.

### Browser Exclusivity Tests

Create `accounts/tests/test_browser_exclusivity.py` with focused registry/guard tests that do not launch Patchright:

- ATP's spec declares `uses_browser=True`.
- A running request job prevents an ATP immediate start.
- A running ATP job prevents a request job immediate start.
- A running request job plus a queued ATP job leaves ATP queued and prevents a later request job from bypassing it.
- A running ATP job prevents queued request jobs from promotion.
- Once no run is live, ATP is promoted alone and no later job is promoted in that dispatch pass.

## Verification

1. Run failing-first ATP and browser-exclusivity tests, then implement.
2. Run `python manage.py test accounts.tests.test_atptour accounts.tests.test_browser_exclusivity --keepdb`.
3. Run `python manage.py test --keepdb`.
4. Run a bounded live Patchright smoke through the assigned datacenter proxy: load ATP's current top-100 rankings page and fetch one player hero JSON from the primed browser context.
5. Run a real current-week ATP scrape through the normal guarded start path and inspect rankings coverage, profile coverage, row count, telemetry, status, progress, and proxy redaction.
6. While that browser run is live, verify a request job stays queued; after ATP completes, verify the queue promotes it.
7. Restart Waitress and confirm `http://127.0.0.1/` returns HTTP 200.

## Non-Goals

- Replacing or reconfiguring `plainproxies.com`.
- Redesigning the global queue, request-thread hysteresis, or scheduler.
- Sharing browser sessions with ITF or College scrapers.
- Persisting ATP browser cookies between runs.
- Changing ATP ranking output schema or scheduling semantics.
- Adding a generic curl fallback for browser scrapers.
