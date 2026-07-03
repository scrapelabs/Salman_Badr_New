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
`match_hash()` only. The DB always persists **only newly-inserted rows** (dedup
via `match_hash`). The run's **items CSV / `row_count` are mode-dependent** on
how the run was invoked (`is_sheet_run` = any input URL on `docs.google.com`):
a **Google Sheet** input emits only the new rows (the production Sheets pipeline
appends the CSV, so re-emitting stored matches would duplicate the sheet); a
**single link / schedule** input emits **every extracted match** (`mapped`,
already in-run de-duplicated) regardless of DB state — a manual re-extract must
hand back the whole box score; the DB is used only to add new matches, never to
filter that CSV. **Why:** the user needs single-link re-extracts to return the
full box score even when all 9 matches are already stored (the "0 new" run that
used to yield an empty CSV). The
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
Sidearm-HTML parsers were deleted at the user's request. HTML is **cleaned then
chunked** before Claude: `_clean_html` strips scripts/styles/nav chrome (a ~490k-char
page → ~17k, every Singles/Doubles row preserved), then it is split at
`CLAUDE_HTML_CHUNK` (160k) chars and each chunk's match list is merged — mirroring the
source's `_extract_text`. **Claude is REQUIRED:** `run()` fails up front with an error
5-tuple when no key (`ANTHROPIC_API_KEY`/per-scraper) OR no prompt is present — by
design, "no big deal" per the user. **Why:** an earlier "send it raw, no cleaning"
request regressed extraction ("not picking up all the scores" — Claude lost score
lines in ~96% noise), so the user reversed it ("check old code, it has everything");
correct data trumps source/instruction literalism here. `CLAUDE_MAX_TOKENS` is 8192
(the source's 4096 barely fit a 9-line dual ≈3.1k out tokens) and `_claude_request`
flags any `max_tokens` stop so a truncated JSON array is never a silent score drop.
OpenAI stays optional (only `_recover_tournament_date`, also via `_clean_html`, gated
on `openai_key`). The browser anti-bot fallback
(`college-browser-fallback` memory) is unrelated and still in place.

**`tournament_url` is stamped, not parsed — easy to lose in a refactor.** The
output column `tournament_url` (col 53) is the source box-score URL, but Claude
never emits it (not in the bespoke 23-key `COLUMNS`, no url mention in the
prompt) and `match_hash` deliberately excludes it. It is populated ONLY by
stamping `row["tournament_url"] = box_url` in `run()`'s `process(box_url)` worker
before mapping, plus `map_extracted()` emitting `g("tournament_url")`. **Why:**
this regressed to a silently-blank column once when a fetch/extraction refactor
dropped the stamp — nothing else fills it, so any rework of the extraction path
must re-stamp the source URL onto each row or col 53 goes blank again.

**Download-by-date export keys off `date_norm`.** The Match-database tab's
"Download by date" panel filters the export by `CollegeMatch.date_norm` (the
indexed normalized ISO `YYYY-MM-DD` *match* date — not `created_at`/scrape time)
via inclusive `date_norm__gte`/`__lte`. This is correct **only** because ISO
date strings sort lexicographically == chronologically — so if `date_norm`'s
format ever changes (or stores non-ISO fallback spellings from dirty imports),
bounded ranges silently mis-include/exclude those rows. Keep `date_norm` ISO, or
switch to a real nullable `DateField` before relaxing that. Blank/invalid `from`
or `to` is leniently ignored (open-ended), and no params = full DB.
