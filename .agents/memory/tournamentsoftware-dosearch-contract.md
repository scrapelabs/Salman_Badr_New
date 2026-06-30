---
name: tournamentsoftware.com DoSearch finder contract
description: The /find/tournament/DoSearch AJAX contract used by all TS-finder scrapers; how it silently returns 0 when the payload drifts.
---

# tournamentsoftware.com tournament-finder (`DoSearch`) contract

Several scrapers discover tournaments by POSTing to the tournamentsoftware.com
finder endpoint `…/find/tournament/DoSearch` (e.g. `itfjuniors`,
`estonia_tournament`, and the shared engines `_ts_tournament` / `_ts_league`).
The finder is an unobtrusive-AJAX form: the find page GET returns an **empty**
`<ul id="searchResultArea">` (results are loaded only when the form is submitted —
the page does NOT auto-search on load).

## The contract (verified June 2026)

- **`LoadMoreResults` is page-2+ only.** Sending `LoadMoreResults=LoadMoreResults`
  means "append the next page of an *existing* search". On a cold session (page 1)
  it returns **HTTP 200 with an empty body (len 0)** → 0 tournaments. The first
  page must be a plain submit (no `LoadMoreResults`); add it only for `Page>=2`.
  Pagination is session-stateful: do page 1 fresh, then `LoadMoreResults`+`Page=n`
  in the **same** client/session.
- **Dates are `datetime-local`:** `StartDate`/`EndDate` must be
  `YYYY-MM-DDTHH:MM` (e.g. `2026-06-26T00:00`). Bare `YYYY-MM-DD` parses to
  nothing → server returns a genuine "No results" (≈115 bytes), masking real data.
- The form also posts `TournamentFilter.YearNr` / `TournamentFilter.MonthNr`
  (blank for a custom date range) and `DateFilterType` is a fixed hidden `0`.
- `X-Requested-With` belongs in the **header**, not the body.

## Failure signature

Empty body (len 0) = `LoadMoreResults` on a cold call. A short ≈115-byte
"No results … choose a different date range" = valid request but the filter
matched nothing (wrong date format, or the window genuinely has no events).

## Data caveat — itfjuniors.tournamentsoftware.com is sparse

This particular host is largely abandoned: data is mostly 2015–2016 plus a
handful in early 2026 (Feb–Mar). Many current windows (incl. mid-2024/2025/2026)
legitimately return **0** tournaments even with a correct payload — that is an
honest empty, not a bug. The bulk of live ITF junior data lives on the
Incapsula-protected itftennis.com family (the patchright/browser scrapers),
which is a different platform. This TS finder uses plain `curl_cffi`
(`ScraperClient`), **no browser** — by design.

**Why:** the legacy ported payloads always sent `LoadMoreResults` and bare dates,
so when the site tightened the contract every TS-finder scraper silently
returned 0 with no error. **How to apply:** if a TS-finder scraper reports 0,
check the payload shape first (LoadMoreResults/date format), then confirm the
window actually has data via a wide-range probe before assuming a code bug.
