---
name: college_dual_match browser render (primary)
description: Why college_dual_match renders every HTML page through one owner-thread patchright browser as the PRIMARY fetch (curl is the fallback), and how the retry/relaunch loop works.
---

# college_dual_match browser render (PRIMARY, not a fallback)

college_dual_match crawls arbitrary public athletics HTML pages (schedules, box
scores) and hands the markup to Claude, which is the parser. **Every HTML page is
now rendered by a real headless patchright Chromium** (full JS executed, the
scraper's proxy attached) so Claude sees the fully-hydrated DOM, not a bare
server response. curl_cffi is the *fallback*, not the primary. (This inverts the
old design, where curl was primary and the browser was a rare anti-bot fallback
gated on `ScraperClient.last_challenge`.)

## The owner-thread renderer (`_BrowserRenderer`)
- **Why a dedicated owner thread, not a lock-shared client:** Playwright's sync API
  is *bound to the thread that started it* — a `goto` from another thread crashes.
  So one long-lived `BrowserClient` is owned by a single daemon thread; phase-2
  worker threads submit `(url, Future)` onto a `queue.Queue` and block on
  `fut.result()`. Renders therefore **serialize** on the owner thread (one Chromium
  is about all the container's memory can spare), while the callers' Claude POSTs —
  the real bottleneck — still run concurrently across the worker pool.
- Lazy launch (first render), persistent profile (`SCRAPER_BROWSER_PROFILE_DIR/<slug>`)
  so clearance cookies survive across pages *and* runs, `allowed_hosts=None` (college
  crawls arbitrary hosts; the SSRF public-IP guard inside `BrowserClient` still applies).
- `close()` pushes a `_RENDER_STOP` sentinel and joins the thread; the owner's
  `finally` calls `client.close()`. `run()` wraps all three phases in try/finally so a
  run never leaves a lingering Chromium — even on an early return or mid-phase error.

## The retry loop (`_render`) — the user's "3 retries" ask
- `BROWSER_RENDER_TRIES = 3`: up to three complete render attempts per URL. The
  **final** attempt (only when >1) first calls `client.relaunch()` (reuses the
  persistent dir, clears stale SingletonLocks) for a fresh context.
- `get_selector()` is single-attempt itself; `_render` is the retry wrapper. Success =
  a non-None Selector whose `.get()` HTML is truthy. Total failure (or a relaunch that
  itself throws) returns an **honest `None`** — never fabricated content.

## `_get_page` (browser-first)
- `renderer.render(url)` → wrap truthy HTML in `_HtmlResponse` (adapter exposing
  `.status_code/.text/.content/.headers` so rendered HTML slots into the curl-Response
  call sites unchanged). On `None` (render unavailable/failed) → one curl_cffi GET.
- **`.pdf` URLs skip the browser** (a `goto` on a PDF aborts as a download) and go
  straight to curl, which also keeps the "response body *is* a PDF" path in
  `_core_extract` working.

## `uses_browser=True` on the college SPEC (now correct)
- Because a run now holds a browser for its whole duration, the college `ScraperSpec`
  sets `uses_browser=True` so views.py's **browser-exclusivity** guard applies (no
  other browser run — e.g. itftennis — starts while a college run is live, and vice
  versa). `uses_browser` is a registry-only attr read live in views.py, **not** a
  Scraper DB field — flipping it needs no migration.

## What stays on curl (intentional, NOT browser-rendered)
- The Google-Sheet CSV export (`_read_google_sheet`), PDF byte downloads
  (`_find_pdf_link`/`_core_extract`), and the Claude/OpenAI API POSTs.
  `BrowserClient.get_selector()` only yields rendered HTML, not binary/API payloads.
