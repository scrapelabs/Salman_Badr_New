---
name: itftennis family uses a stealth browser (patchright) for the Incapsula challenge
description: Why the itftennis.com scrapers need a real browser not curl_cffi, the counterintuitive proxy/direct fact, and the Playwright SSRF blind-spots a browser HTTP client must close.
---

# itftennis.com — phase-2 needs a stealth browser, and the proxy is the problem

The `itftennis.com` family (`_itftennis.py`, circuits juniors/masters/mens/womens)
sits behind **Imperva/Incapsula** (JS challenge, not a plain IP block). The
two-phase design:
- **Phase 1 (discovery)** — `GetCalendar` / single-URL listing — still works with
  plain `curl_cffi`.
- **Phase 2 (per-tournament scrape)** — the tournament page + its `TournamentApi`
  calls — gets the Incapsula JS challenge and must be fetched with **patchright**
  (a stealth fork of Playwright) driving a real Chromium/Chrome, which executes the
  challenge JS and earns the cookies. The API calls then run as **in-page
  `fetch()`** (see next section). This lives in `_browser.py` (`BrowserClient`);
  see the "stealth config" section for the persistent-profile / headed setup.

## The API calls MUST be in-page `fetch()`, not `context.request` (the 2026-06-26 regression)

When ITF added Incapsula on the **API** (not just the pages), the scraper went
"all requests succeed, 0 rows". Root cause: phase-2 API calls used
`context.request.get()` — Playwright's out-of-page `APIRequestContext`. It shares
the context's **cookies** but runs **outside the page JS**, so Incapsula still
challenges it: HTTP **200** with a tiny interstitial body (~212–659 bytes) that
isn't JSON → `get_json` returns `None` → 0 matches. `page.goto` works (it runs the
challenge JS) so the pages looked fine — masking the real failure.

**Fix:** issue each API GET as an **in-page `fetch()`** via `page.evaluate`
(`fetch(url,{credentials:'include'})` → `{status, body}`), from a page already
navigated to the same origin (`www.itftennis.com`). It inherits the solved
clearance cookie/token **and** the real browser fingerprint (UA, Referer,
`sec-fetch-*`) that the bare client can't reproduce → real JSON.

**A/B proof (2026-06-26, Replit direct IP, page challenge solved):** same browser
session, same `GetEventFilters` URL — `context.request` → HTTP 200 / 659-byte
challenge; in-page `fetch()` → real JSON (`tourType`, filters). One central fix in
`_browser.py::_fetch` covers all 4 circuits (they share `_itftennis.py`).

**How to apply:** any patchright-backed scraper hitting a JS-challenged JSON/XML
API must fetch from inside the page, never via `context.request`. The page must be
on the API's origin first (get_selector the tournament page), else the fetch is
cross-origin (CORS) and/or unchallenged-but-wrong-origin.

**Caveat (still IP-gated):** if the *page* challenge itself doesn't clear (raw
datacenter IP, intermittent — seen the same day: get_selector → "anti-bot
challenge HTTP 200" → 0 tournaments), nothing downstream works. That's the
residential-proxy issue below, orthogonal to in-page-vs-out-of-page.

**Counterintuitive operational fact (NOT derivable from code):** itftennis works
**DIRECT from Replit with no proxy**. It is the *residential proxy IP* that gets
Incapsula-challenged/0-rows — the opposite of the Stadion/CloudFront family
(`cloudfront-datacenter-block.md`), where the datacenter IP is blocked and you
*need* a residential proxy. So: Stadion → assign a proxy; itftennis → leave the
proxy off (direct). Re-confirmed 2026-06-25: Replit direct + headless Chromium
solved the challenge and fetched the real 251 KB itftennis homepage, 0 errors.

**Why:** Incapsula is a JS-execution challenge tied to IP reputation; the proxy
pool's IPs are flagged, Replit's egress (for this host) is not. curl_cffi TLS
impersonation alone can't pass a JS challenge — you need a real JS engine.

# Stealth config: persistent profile + headed + real Chrome (OS-aware)

patchright's recommended max-stealth setup is **launch_persistent_context** (not
`launch()`+`new_context()`) + a real Google **Chrome** channel + **headed** mode +
`no_viewport=True` + no extra automation `--flags`. `BrowserClient` takes
`channel`/`user_data_dir` and reads OS-aware, env-overridable defaults from settings
(`SCRAPER_BROWSER_HEADLESS` / `_CHANNEL` / `_PROFILE_DIR`):
- **Local Windows** → headed + `channel="chrome"` + per-scraper persistent profile.
- **Replit Linux** → headless Chromium (`executable_path`); no real Chrome, no X
  display — same persistent-profile path, degrades automatically.

**Why persistent:** the Incapsula clearance cookie is earned once and reused, so a
stable per-scraper profile dir (`<SCRAPER_BROWSER_PROFILE_DIR>/<slug>`, git-ignored)
cuts re-challenges. Per-slug dirs avoid two circuits sharing one *locked* Chrome
profile; a crashed run's stale `SingletonLock/Cookie/Socket` are wiped before
relaunch (safe under the one-running-run-per-scraper DB constraint).

**Operator note:** don't manually open Chrome pointed at a scraper's profile dir
while a run might start — that's the one case the stale-lock wipe shouldn't hit.

**Persistent-context API gotcha:** `launch_persistent_context` returns the
BrowserContext directly (no separate `Browser` — `context.close()` tears down the
Chrome process); it opens with a default page (reuse `context.pages[0]`). All 4 SSRF
guards attach to this context exactly as before.

**Page-load settle (don't read on `domcontentloaded`):** `goto` waits only for
`wait_until="domcontentloaded"`, which returns the instant the HTML is parsed —
*before* subresources and JS/XHR-driven content render — so reading `page.content()`
straight after gets a half-loaded page (the symptom: "chrome is not waiting until
page load", short/empty content). After `goto` (and after the challenge auto-reload
beat) call a `_settle()` that waits for the full `load` event then `networkidle`.
**Both waits MUST be tolerant** (swallow timeouts): a page with persistent
polling/websockets never reaches `networkidle`, so a hard wait would hang/fail the
read — on timeout just fall through to whatever has loaded. Bound by
`settle_timeout` (default 20s). Confirmed 2026-06-26: settled read of the itftennis
homepage returned the full ~282 KB document (vs ~251 KB on the bare `domcontentloaded`
read), valid `<title>`, 0 errors.

# Per-request rotation (the DEFAULT as of 2026-06-25): fresh browser + IP per tournament

A single persistent identity still gets Incapsula-re-challenged "after a few
records", so phase 2 now defaults to **rotation**: open a brand-new
`BrowserClient` (fresh fingerprint + a *throwaway ephemeral* profile, so no
carried cookie) for **each tournament**, inside its own `with` block, then close
it. Toggle with `SCRAPER_BROWSER_ROTATE_PER_REQUEST` (default True; `=False`
reverts to the one-persistent-session path above).

- **Granularity = per tournament, NOT per HTTP call.** The tournament page solves
  the challenge once; its `TournamentApi` calls must reuse that browser's solved
  cookie (and IP), so relaunching per API call would break the session and is
  pointless. "Each request" the user means = each tournament (= each "record").
- **Fresh IP needs infra.** A rotating *gateway* proxy gives a new exit IP per new
  connection (each relaunch) on its own; a *sticky-session* residential provider
  needs a `{session}` (or `{rand}`) placeholder in the `Proxy.address`
  (e.g. `http://user-session-{session}:pass@gw:7000`) — `browser_proxy(session=)`
  substitutes a fresh `secrets.token_hex(8)` per launch. **Direct (no proxy)
  rotates fingerprint only, not IP.** The token/address are never logged.
- **Rotation bypasses the persistent profile** (`user_data_dir=None` → ephemeral
  temp dir, deleted on close) — that's the whole point: shed the identity. The
  persistent-profile / per-slug-dir machinery above applies only when rotation is
  OFF.
- **Cost:** ~3-4s warm Chrome relaunch + a fresh challenge solve per tournament
  (first launch ~14s cold). Accepted as the price of evading the re-challenge.
- Per-tournament launch failures are caught + recorded + increment progress once
  and continue (one bad launch ≠ dead run); `progress_done` stays exactly-once
  per tournament (crawl_one's `finally` on success XOR the launch `except`).
- **Rotation parallelises (added 2026-06-26).** Because each tournament is a fully
  isolated browser, rotate-mode phase-2 fans out across `Scraper.threads` worker
  threads — a `ThreadPoolExecutor(max_workers=workers)` running one
  `crawl_isolated(tournament)` (own `make_browser()`) per task, gated by
  `workers > 1 and len(tournaments) > 1` (one tournament → serial; no pool). Each
  thread drives its **own** browser (sync Playwright is one-instance-per-thread).
  The **non-rotate** path keeps one shared persistent browser and is **inherently
  sequential** whatever `threads` is (a single Playwright page can't be driven from
  many threads). **OOM caveat:** `threads` clamps 1–16; N concurrent headless
  Chromium is RAM-heavy on Replit — default 5 is fine, 16 is a real OOM/kill risk.
  First operational mitigation is lowering `Scraper.threads`, not a code change.

# Playwright SSRF blind-spots (the parity gotchas)

A browser HTTP client needs the same SSRF protection as the curl client
(`assert_safe_url` + host allowlist), but Playwright's interception has holes you
must plug explicitly — `context.route("**/*", guard)` alone is **not** enough:

1. **In-page `fetch()` (the API path) DOES flow through `context.route`** — unlike
   the old `context.request`, every hop (incl. redirects) hits `_route_guard`
   (public-IP check). Still `assert_safe_url(full_url, allowed_hosts=)` the
   resolved target up front in `_fetch` before `page.evaluate`. (If you ever
   reintroduce `context.request`: it bypasses page routes — then you must follow
   redirects by hand with `max_redirects=0` + per-hop `assert_safe_url`.)
2. **WebSocket handshakes are NOT intercepted by `context.route`.** Register a
   separate `context.route_web_socket("**/*", …)` (available in modern
   patchright/Playwright) and `close()` any ws whose host fails the public-IP
   check; `connect_to_server()` to passthrough the safe ones.
3. **Service-worker-originated requests bypass `context.route`.** Create the
   context with `service_workers="block"`.
4. The page-route guard must **fail closed** (default `abort`, only `continue_`
   after an explicit pass) — a guard that fails open silently widens SSRF.
5. Asymmetry that's correct: enforce the **host allowlist** only on the top-level
   document + its redirect hops (`is_navigation_request() and
   frame.parent_frame is None`); for subresources enforce **public-IP-only** (no
   allowlist) so legit cross-host CDNs load but nothing reaches a private address.

**How to apply:** validate a browser scraper offline with a stub route object
(assert allowed-host nav→continue, internal/off-allowlist/private→abort,
image→abort), then one live run to confirm the hardened context still solves the
challenge (M25 Deauville date-range ≈ 70 rows / 0 errors is the known-good smoke).
patchright needs its Chromium: `pkgs.chromium` (replit.nix) on Replit and
`patchright install chromium` for local Windows.

# Playwright sync API + Django ORM = SynchronousOnlyOperation (the real-worker trap)

Playwright's **sync** API drives an asyncio event loop in the calling thread. Once
`sync_playwright().start()` runs, Django's `async_unsafe` guard sees a running loop
and raises **`SynchronousOnlyOperation`** on *every* ORM call made while the browser
is open — i.e. the worker's own `log()`/telemetry `RunLogLine` writes that stream the
live console. So the browser block dies the moment it logs its first line.

**Fix:** set `DJANGO_ALLOW_ASYNC_UNSAFE=1` for the browser phase. The env var is
**process-global**, so the scope matters once phase-2 is concurrent (see rotation
section): a per-`BrowserClient` set-in-`__enter__`/restore-in-`close()` **races**
across threads (one thread's `close()` clears the var while another's browser is
still live → `SynchronousOnlyOperation`). So lift it to **one phase-level
contextmanager** `allow_async_unsafe()` (set/restore exactly once for the whole
block) and have each client pass `manage_async_unsafe=False` so it never touches
the var itself (`BrowserClient` still self-manages when `True` for any
single-shot/non-`run_scrape` caller). It's the official Django escape hatch and is
safe here: the var is scoped to a one-shot worker phase, **never** a long-lived
multithreaded web-server process — keep it that way.

**Validation gap that hid the ORM trap (the expensive lesson):** an in-process
smoke test whose `log` is a no-op / list-append stub **never touches the ORM**, so
it passes even though the real worker (whose `log` writes a `RunLogLine`) blows up.
Any browser-backed scraper MUST be validated with a `log` that actually issues an
ORM query — never a stub. For the **concurrent** path the bar is higher: launch N
*real* headless Chromium at once (prove overlap with a `threading.Barrier`) and do
a real ORM write (RunLogLine + `Run` `F()+1`) inside each thread while its browser
is live; include a **negative control** (a `manage_async_unsafe=False` client with
NO wrapper must raise `SynchronousOnlyOperation`) so you know the guard is real,
not a no-op. Confirmed 2026-06-26: 3 concurrent browsers, all ORM writes OK, no
race, env restored to unset after every scope; negative control raised as expected.
Earlier single-thread confirmation: single-URL M25 Deauville via runserver →
success, 31 rows, 86 ORM-streamed log lines, 0 async errors.

# Player-DOB (`GetHeadToHeadPlayerDetails`) is its own anti-bot battle (Option A, 2026-06-26)

The per-player DOB XML endpoint is gated **separately** from the drawsheet:
- **It's JS-challenge-gated on the FIRST hit.** Probe: direct `curl_cffi` on the DOB
  URL = 29/30 CHALLENGE on request #1. So DOB can't be fetched out-of-browser, and
  "fresh IP per request via curl / fresh-IP-per-request" is **useless** — it must come
  from inside the Incapsula-cleared patchright browser (in-page fetch, like the API).
- **But a burst trips a RATE re-challenge** even while `TournamentApi` still works:
  dozens of DOB fetches/sec from one session re-challenge that one identity.

**Chosen design (Option A — NOT per-request relaunch):** pace each DOB lookup (a
short `time.sleep`, `SCRAPER_ITF_DOB_DELAY_MS`) so the per-IP rate stays under the
threshold → most resolve in the *same* browser, fast. Only when a lookup is *still*
blocked, **relaunch** the browser (`BrowserClient.relaunch()` → fresh exit IP on a
rotating proxy + re-solved clearance) and retry, bounded by
`SCRAPER_ITF_DOB_MAX_ROTATIONS`. After the budget is spent, DOB is **best-effort
blank** so a match row is never lost / a run never stalls over a stubborn DOB.
**Why not relaunch-per-DOB:** a relaunch is ~3-4s + a fresh challenge solve;
per-call rotation would make a tournament take hours and throw away the clearance the
very next call needs.

**`BrowserClient.relaunch()` env-var ownership (the trap):** `relaunch()` =
`_teardown_browser()` + `_launch()`, and must **NOT** touch
`DJANGO_ALLOW_ASYNC_UNSAFE`. That var is owned once by `__enter__`/`close()`; if a
relaunch in one worker released it, sibling threads' live browsers would start raising
`SynchronousOnlyOperation`. So the lifecycle is split: `_launch` (build context, no
env-var, exceptions propagate) / `relaunch` (teardown+launch, no env-var) / `close`
(teardown + env-var restore) / `_teardown_browser` (context/pw close + drop ephemeral
profile, no env-var). A failed `relaunch` leaves the client **browser-less** — it then
returns honest `None`s (→ blank DOB) until `close()`, which is fine.

**Shared DOB cache write race (architect catch):** the per-run cache is checked under
a lock, the lookup runs *outside* the lock, then the result is written — so two threads
can miss the same player and a best-effort **blank** from one can clobber a **good**
DOB the other just cached, poisoning every later row for that player. Fix: re-check
under the lock before writing and **never overwrite a non-empty cached DOB with an
empty result**. (General rule for any "check cache → slow fetch → write cache" with a
best-effort/blank failure value.)
