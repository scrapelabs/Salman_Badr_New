---
name: college_dual_match browser fallback
description: How/why college_dual_match falls back to a patchright persistent-profile browser only on genuine anti-bot challenges, via ScraperClient.last_challenge.
---

# college_dual_match conditional browser fallback

college_dual_match fetches arbitrary public athletics HTML pages (schedules, box
scores) over the shared curl_cffi `ScraperClient`. A few hosts (e.g. sammieetc.com)
sit behind a JS anti-bot interstitial that 403s the HTTP client. When — and only
when — curl hits a genuine **challenge**, the scraper retries that one page through a
patchright `BrowserClient` with a **persistent** profile.

## The `ScraperClient.last_challenge` flag
- `_http.ScraperClient` exposes `self.last_challenge` (init `False`, reset `False` at
  the top of `request()`, set `True` inside `_fetch_one` when `_is_challenge(resp)`).
- Lets a caller distinguish "anti-bot give-up" from any other failure (timeout, 404,
  5xx) so it can fall back selectively instead of browser-launching on every miss.
- **Semantics:** it means "*any* attempt during this request saw a challenge," not
  strictly "the final failure was a challenge." Architect flagged this as slightly
  over-broad (a host that challenges then returns a clean non-2xx within the retry
  budget would trigger one wasted Chromium launch that then honest-fails). Left as-is
  on purpose: rare, safe, and erring toward the browser matches the user's intent.
  Don't add per-attempt reset state to the hot retry loop unless a real case appears.
- Backward-compatible: every other scraper ignores the flag.

## The fallback wiring (college_dual_match.py)
- `_HtmlResponse` — tiny adapter exposing `.status_code/.text/.content/.headers` so
  browser-rendered HTML slots into the existing curl-Response call sites unchanged.
- `_BrowserFallback` — holds scraper/log/tele + a persistent `profile_dir` and a
  **module `threading.Lock`**; `fetch_html(url)` serializes browser use to **one
  Chromium at a time** (Playwright sync is single-thread-bound; a persistent profile
  dir can only be opened by one process via its SingletonLock). Opens
  `BrowserClient(user_data_dir=<persistent dir>, allowed_hosts=None)`, returns
  `sel.get()` (full reserialized HTML) or `None` on honest fail.
- `_get_page(client, url, ..., browser)` — try curl; if `resp` is `None`/non-2xx **AND**
  `client.last_challenge` **AND** a browser was passed → `browser.fetch_html`, wrap in
  `_HtmlResponse`. Threaded `browser=` through `_discover`/`_crawl_schedule`/
  `_extract_box_score`; `run()` builds the fallback once
  (`profile_dir = SCRAPER_BROWSER_PROFILE_DIR/<slug>`) and passes it into phase-1
  discovery and the phase-2 ThreadPoolExecutor (up to 16 worker threads, all serialized
  through the one lock).

## Persistent vs ephemeral profile
- College uses a **persistent** profile (no rotate) so anti-bot clearance cookies
  survive across re-opens — opposite of itftennis (`_itftennis.py`), which uses an
  ephemeral profile + per-tournament IP rotation. See `itftennis-patchright-browser.md`.

## Why `uses_browser=False` on the college SPEC (deliberate)
- Browser here is a **conditional**, lock-serialized (one Chromium) fallback, not a
  browser-native engine. The existing browser-exclusivity guard already blocks the only
  dangerous overlap (itftennis can't start while any run is live; nothing can start
  while a browser run is live), so the worst case is one fallback Chromium beside other
  curl scrapers — not itftennis's multi-browser pool.
- Flagging `uses_browser=True` would needlessly serialize all college **pure-curl**
  runs. Keep it `False` unless real memory pressure shows up.

## What is NOT browser-wrapped (intentional)
- PDF box-score fetches and the auburn stats-XML fetch (browser returns HTML text, would
  corrupt a binary PDF), the Google-Sheet CSV export, and the Claude API POST. Those stay
  on curl — `BrowserClient.get_selector()` only yields rendered HTML, not binary/API
  payloads.
- The curl challenge attempt still records a telemetry error even when the browser then
  succeeds — honest and intentional (slightly noisy errors.csv for recovered pages).
