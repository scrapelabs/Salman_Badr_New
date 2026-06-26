---
name: College match store & dedup
description: How college_dual_match results persist + dedup; the cross-source-collapse decision and the has_match_store tab pattern.
---

College Dual Match results persist to a dedicated DB table (separate from the
per-run CSV blobs every other scraper uses). All writes — live scrapes AND the
historical-CSV importer — funnel through one module (`accounts/college_store.py`)
so the model, the run's items CSV, the Lab "Match database" tab, and the bulk
importer all agree on the column set, dedup key, and CSV format.

**Dedup = normalized *identity* hash, not full-row hash.** The dedup key digests
only identifying fields (date, gender, draw, sorted player pairs, score, teams),
each normalized (ISO date, lower-cased, score whitespace stripped, doubles pairs
sorted). Volatile/metadata fields (tournament_url, third-party ids, cities, DOBs)
are deliberately excluded.

**Why (the decision the user can override):** the *same real match* is reported
by both schools' athletics sites with different URLs and date spellings
(`05/24/2026` vs `5/24/2026`). A full-row hash would store both as two matches;
the identity hash **collapses cross-source duplicates to a single row**. This is
intentional and was surfaced to the user. If they ever want to keep one row
*per source*, add a source-identity component (e.g. host/url) back into
`match_hash` — see the `scraper-ssrf-and-dedup` memory's converse warning about
dedup keys that are *too* loose dropping legitimately-distinct rematches.

**How to apply:** any change to what counts as "the same match" lives in
`match_hash()` only. A run's items CSV / `row_count` report **only that run's
newly-inserted rows** (not everything scraped) — keep that contract. The
prefilter-then-`bulk_create(ignore_conflicts=True)` attribution is exact because
scrapes are single-in-flight per scraper and imports are a manual CLI step (no
concurrent ingest); if that ever changes, switch to `ON CONFLICT ... RETURNING`.

**has_match_store flag pattern:** a `ScraperSpec.has_match_store` bool gates the
whole feature — the nav tab, the `?tab=data` view branch (redirects otherwise),
and the `matches.csv` export (404 otherwise). Only `college_dual_match` sets it.
To give another scraper a match database, set the flag + have its runner ingest
through `college_store` (the table/columns are currently college-shaped).
