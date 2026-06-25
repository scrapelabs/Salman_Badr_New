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
  (a stealth fork of Playwright) driving headless Chromium, which executes the
  challenge JS and earns the cookies. `context.request` then reuses those solved
  cookies for the API calls. This lives in `_browser.py` (`BrowserClient`).

**Counterintuitive operational fact (NOT derivable from code):** itftennis works
**DIRECT from Replit with no proxy**. It is the *residential proxy IP* that gets
Incapsula-challenged/0-rows — the opposite of the Stadion/CloudFront family
(`cloudfront-datacenter-block.md`), where the datacenter IP is blocked and you
*need* a residential proxy. So: Stadion → assign a proxy; itftennis → leave the
proxy off (direct). `BrowserClient(proxy=None, headless=True)`.

**Why:** Incapsula is a JS-execution challenge tied to IP reputation; the proxy
pool's IPs are flagged, Replit's egress (for this host) is not. curl_cffi TLS
impersonation alone can't pass a JS challenge — you need a real JS engine.

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
