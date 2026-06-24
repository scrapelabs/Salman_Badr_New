---
name: ITF/Stadion API CloudFront datacenter-IP block
description: Why the BJK Cup / ITF-Stadion scraper returns 403/0 rows without a residential proxy, and what the client must do to match production.
---

# ITF / Stadion API is behind CloudFront — datacenter IPs get 403

The upstream API `api.itf-production.sports-data.stadion.io` (used by the
Billie Jean King Cup scraper and any sibling ITF/Stadion team competition) sits
behind **CloudFront**. CloudFront returns `403 "Request blocked"` with
`server: CloudFront` + `x-cache: Error from cloudfront` to **datacenter / cloud
IP ranges**, including Replit's. This is a WAF/IP-reputation block at the CDN
edge — it fires *before* the origin, so no header, cookie, or TLS trick alone
gets past it.

**Why:** the request is rejected purely on source-IP reputation. Browser TLS
impersonation (`curl_cffi impersonate="chrome"`) is necessary to match the
production client but is **not sufficient** on its own — a direct request from
Replit's IP still 403s even with impersonation. The only thing that works is
routing through a **residential / working proxy**. This mirrors the user's
production framework, which uses `curl_cffi` (`use_cffi=True`) **and**
`settings.PROXIES` (`use_proxy=True`).

**How to apply:** when a Stadion/ITF (or similar CDN-fronted) scraper returns
0 rows / 403:
1. Don't chase headers/UA — confirm it's a CloudFront edge block (check
   `server`/`x-cache` response headers).
2. The HTTP client must be `curl_cffi` with Chrome impersonation (parity with
   production), routing through the scraper's assigned proxy.
3. A **residential proxy must be assigned** to the scraper (MatchMiner: Lab →
   Settings tab; proxies are managed on the Proxies page). Without one it fails
   honestly (telemetry records the real 403). With one, BJK Cup 2026 returns
   ~478 rows in ~1.5 min.
