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
  challenge JS and earns the cookies. `context.request` then reuses those solved
  cookies for the API calls. This lives in `_browser.py` (`BrowserClient`); see the
  "stealth config" section for the persistent-profile / headed / real-Chrome setup.

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

1. **`context.request` bypasses page routes entirely.** Validate its redirects by
   hand: `max_redirects=0` + `assert_safe_url(..., allowed_hosts=)` on every hop.
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
