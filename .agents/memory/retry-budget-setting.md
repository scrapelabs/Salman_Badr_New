---
name: Per-request retry budget setting
description: How the per-scraper "total tries per request" knob is plumbed; every HTTP client default must derive from one central source or it silently won't honor the setting.
---

The Lab Settings tab exposes a per-scraper **retry budget** (`Scraper.max_tries`, clamped 1..10) alongside proxy + threads. It controls how many total attempts each HTTP request makes before giving up.

**The rule:** the budget is applied **process-wide** via a module-global in `_http` (`set_default_tries()` / `get_default_tries()`), set **once per run** by the `run_scrape` worker (`handle()` calls `_http.set_default_tries(scraper.effective_tries)` right after loading the scraper, before dispatch). Every HTTP client's *default* try count must derive from `_http.get_default_tries()`:
- `ScraperClient` (`tries=None` → `get_default_tries()`),
- the standalone Stadion `_get_json` helper,
- the Playwright `BrowserClient` (`api_tries=None` → `get_default_tries()`).

**Why a module-global is safe here:** every run is a **fresh subprocess** (web/webhook via `subprocess.Popen`, the local bat via `scrape_now` → `call_command("run_scrape")` in its own command process), so there is no cross-run leakage. The worker's `run_scrape.handle()` is the **single choke point** that ALL entry paths funnel through, which is why setting it there covers web + webhook + bat.

**How to apply:** any *new* HTTP helper/client you add must default its try count to `get_default_tries()` (accept an explicit override that still wins), or the per-scraper setting silently won't govern it — exactly the gap the architect caught with `BrowserClient` initially. Do **not** hardcode per-call `tries=N` in a runner (padelfip/atptour used to and bypassed the knob); let calls inherit the client default so the setting is uniform. Explicit `tries`/`api_tries` are clamped to `>=1` defensively.
