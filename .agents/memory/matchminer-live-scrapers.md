---
name: MatchMiner live scrapers (which competitions are feasible)
description: Only Billie Jean King Cup is currently wired; why the catalogue was trimmed to it, and the shared Stadion API behind it.
---

# Which tennis sources can be scraped live (and why most can't)

The catalogue was **deliberately trimmed to a single wired scraper —
Billie Jean King Cup** (Fed Cup) — to perfect it before adding others. It rides
the public **ITF/Stadion JSON API** (`api.itf-production.sports-data.stadion.io`):
60-col item schema, parameterised by draw code (`bjkc`), gender, and match URL.
That API needs **no proxies and no credentials**, so a stdlib `urllib` port works
in the Replit environment.

**Why:** **Davis Cup** (draw code `dc`, men) was previously wired too but was
removed at the user's request ("remove other junk besides BJK Cup; perfect that
one, add others afterward"). It is NOT gone for technical reasons — the shared
`_stadion.py` engine is still parameterised (`StadionConfig`), so Davis Cup (or
any other Stadion-backed competition) can be re-added later as a thin wrapper
+ a seed row + a `LIVE_SCRAPERS` registry entry. The user's original framework
(~40 spiders) relies on a rotating proxy pool + `curl_cffi` + Selenium + AI
extraction for non-Stadion sources (federation sites with bot protection); none
of that infra is available here, so those would fabricate data or fail.

**How to apply:**
- The app **fails honestly** for any slug with no `LIVE_SCRAPERS` entry — never
  simulate/fabricate rows.
- To add a real scraper, prefer another Stadion-backed competition (thin wrapper
  over `_stadion.py` with a new `StadionConfig`). For a non-Stadion source you
  first need the proxy/credential infra — don't fake it.
- Trimming the catalogue is two parts: edit the seed migration so fresh DBs only
  seed the kept scraper(s), AND add a data migration that deletes the rest from
  existing DBs (their `Run` rows cascade via the `Run.scraper` FK).
- Per-run telemetry (requests/errors CSVs) formats must stay byte-compatible with
  the production framework; see `telemetry.py`.
