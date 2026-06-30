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

**Direct box-score link + Claude-ONLY extraction.** `college_dual_match`
accepts a *direct* match-page URL (a Sidearm boxscore, e.g. cmsathletics.org), not
just a Google Sheet / `/schedule` page — `_discover` classifies a single boxscore
as one recap. Extraction is **Claude-only**: every box score (HTML or PDF) goes to
Claude. **There is NO deterministic fallback** — the old auburn stats-XML and
Sidearm-HTML parsers were deleted at the user's request. HTML is sent **raw / as-is**
(no `_clean_html`, no chunking; a ~490k-char page ≈ ~155k input tokens, inside the
200k context). **Claude is REQUIRED:** `run()` fails up front with an error 5-tuple
when no key (`ANTHROPIC_API_KEY`/per-scraper) OR no prompt is present — by design,
"no big deal" per the user. **Why:** the user wanted the pipeline dead-simple (raw
page → Claude → done) over best-effort coverage. OpenAI stays optional (only
`_recover_tournament_date`, which is why `_clean_html` survives — it cleans HTML
before the OpenAI date call, gated on `openai_key`). The browser anti-bot fallback
(`college-browser-fallback` memory) is unrelated and still in place.

**Download-by-date export keys off `date_norm`.** The Match-database tab's
"Download by date" panel filters the export by `CollegeMatch.date_norm` (the
indexed normalized ISO `YYYY-MM-DD` *match* date — not `created_at`/scrape time)
via inclusive `date_norm__gte`/`__lte`. This is correct **only** because ISO
date strings sort lexicographically == chronologically — so if `date_norm`'s
format ever changes (or stores non-ISO fallback spellings from dirty imports),
bounded ranges silently mis-include/exclude those rows. Keep `date_norm` ISO, or
switch to a real nullable `DateField` before relaxing that. Blank/invalid `from`
or `to` is leniently ignored (open-ended), and no params = full DB.
