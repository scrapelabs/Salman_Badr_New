---
name: MatchMiner live scrapers (which competitions are feasible)
description: Why only BJK Cup + Davis Cup are wired as real scrapers, and the shared API behind them.
---

# Which tennis sources can be scraped live (and why most can't)

Only **Billie Jean King Cup** (Fed Cup) and **Davis Cup** are wired as real
scrapers in MatchMiner. Both ride the **same public ITF/Stadion JSON API**
(`api.itf-production.sports-data.stadion.io`) — same endpoints and same 60-col
item schema, differing only by draw code (`bjkc` vs `dc`), gender, and the
match URL. That API needs **no proxies and no credentials**, so a stdlib
`urllib` port works in the Replit environment.

**Why:** the user's original framework (`scripts_*.zip`, ~40 spiders) relies on a
rotating proxy pool + `curl_cffi` + Selenium + AI extraction for the other
sources (federation sites with bot protection). None of that infra is available
here, so wiring those would fabricate data or fail. We chose to **fail honestly**
for unwired slugs rather than simulate.

**How to apply:** to add another real scraper, prefer another Stadion-backed
competition (thin wrapper over `_stadion.py` with a new `StadionConfig`). For a
non-Stadion source, you first need the proxy/credential infra — don't fake it.
Per-run telemetry (requests/errors CSVs) formats must stay byte-compatible with
the production framework; see `telemetry.py` (the spec was reverse-engineered
from the user's attached sample CSVs).
