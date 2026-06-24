---
name: Proxy credential handling
description: How proxy addresses (which may carry credentials) must be rendered, logged, and used to route scraper traffic in MatchMiner.
---

# Proxy credentials

A `Proxy.address` may embed credentials in `user:pass@host:port` form (optionally
with a scheme like `http://` or `socks5://`).

## Rules
- **Never render the raw `address` in any template or UI.** Always render
  `Proxy.display_address`, which masks the password segment with bullets.
- **Scrapers must never log the address** — log only the proxy's name and type.
  The masked form is fine in UI, but logs should not even include the masked
  address.
- Routing: a scraper uses its assigned proxy only when the proxy is active **and**
  has a non-empty address. Direct mode builds an explicit empty `ProxyHandler({})`
  so it ignores ambient `HTTP(S)_PROXY` env vars.

**Why:** rendering the raw address leaks embedded credentials in the UI. Masking
on display + never logging the address is the standing convention for this app.

## How to apply
Any new code that displays a proxy address → use `display_address`. Any new scraper
that honours a proxy → thread the opener, never log the address, and gate on
active + non-empty address.

## Implementation gotcha
The masking uses `re.sub`. A regex **replacement template** cannot contain a
`\u2022` escape — Python's `re` parser raises `re.error: bad escape \u`. Use a
literal bullet character in the replacement plus `\g<1>`/`\g<2>` group references
(named/numbered backrefs are fine; arbitrary `\u` escapes are not).
