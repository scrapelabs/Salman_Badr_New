---
name: Scraper SSRF hardening & row dedup rules
description: Two durable rules for the MatchMiner live scrapers — how URL-input scrapers must guard against SSRF (incl. redirects) and how to key row dedup so rematches aren't silently dropped.
---

# SSRF: validate every hop, not just the seed

URL-input scrapers (e.g. `croatia_league`, future `cosat`) accept a user/webhook-supplied
tournament URL. Two layers guard against SSRF, both centralised in
`accounts/live_scrapers/_ssrf.py` (`assert_safe_url` / `assert_resolves_public`):

1. **Seed validation** (`views._validate_tournament_url`) — scheme http(s), reject IP
   literals + local names, enforce the per-spec host allowlist, AND resolve the host and
   reject any private/loopback/link-local/reserved/multicast result.
2. **Redirect validation** (`_http.ScraperClient`) — the client sets `allow_redirects=False`
   and follows redirects **manually**, re-validating each hop with `assert_safe_url` (public-IP
   only; no allowlist) before fetching it. 301/302/303 drop to a bodyless GET; 307/308 preserve.

**Why:** validating only the seed is a hole — an allowlisted host can serve a 30x to
`169.254.169.254` / `127.0.0.1` / a private IP, and `curl_cffi` follows redirects by default,
so the request fires at the internal target. A *post-hoc* check of `resp.url` is too late (the
SSRF already happened). Resolving the host (not just `ipaddress.ip_address`) is what defeats
numeric-host obfuscation (`http://2130706433/`, `http://0x7f000001/` → 127.0.0.1).

**How to apply:** never re-enable auto-redirects in `ScraperClient`. Redirect re-validation
uses public-IP safety only (NOT the per-spec allowlist) on purpose, so legitimate cross-host
/ CDN redirects still work; the allowlist stays enforced at seed validation. Any new URL-input
scraper inherits this for free by going through `ScraperClient` + `validate_run_params`.

# Row dedup must key on source identity, not player+score

Each scrape dedups rows via a `seen` set. The key MUST include source identity —
`match_url` (Croatia) or `tournament_url` (Brazil) **plus** `draw_name`, `round`, `date` —
in addition to the player names + score.

**Why:** an early version keyed only on `(draw_name, winner/loser names, score)`. League play
produces genuine **rematches** (same two players, same score like `6-0, 6-1`, different
day/round); a coarse key silently collapses them → undercounted CSV (data loss). `match_id`
is often empty from these sources, so it can never be relied on alone.

**How to apply:** when adding a scraper, build the dedup key from the most specific source
locator available (the match/tournament URL) + date + round + names + score. The goal is to
drop only exact re-enumerations of the *same* match, never two distinct matches that happen to
share a scoreline.
