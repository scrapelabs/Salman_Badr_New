---
name: tournamentsoftware.com DoSearch finder contract
description: The /find/tournament/DoSearch AJAX contract; the STRICT variant on the itfjuniors host returns 0 on a drifted payload, while sibling TS hosts tolerate the legacy payload (verify per-host).
---

# tournamentsoftware.com tournament-finder (`DoSearch`) contract

Several scrapers discover tournaments by POSTing to the tournamentsoftware.com
finder endpoint `…/find/tournament/DoSearch` (e.g. `itfjuniors`,
`estonia_tournament`, and the shared engines `_ts_tournament` / `_ts_league`).
The finder is an unobtrusive-AJAX form: the find page GET returns an **empty**
`<ul id="searchResultArea">` (results are loaded only when the form is submitted —
the page does NOT auto-search on load).

## Scope — host-specific; sibling TS scrapers are NOT broken

The strict behavior below is **only** confirmed on
`itfjuniors.tournamentsoftware.com`. Every other tournamentsoftware.com host
tested **tolerates the legacy payload** (`LoadMoreResults` on page 1 + bare
`YYYY-MM-DD` dates) and returns full results. Verified live (June 2026) with the
**current shipped code**, cold session, no proxy:

- `_ts_tournament` engine — svtf **485** / ireland(ti) **701** tournaments; te/etl
  page-1 returns 20 with the legacy payload.
- `estonia_tournament` (etl host) — **487** tournaments.
- `_ts_league` engine (`/find/league/DoSearch`, `LeagueFilter.*`, **no**
  `DateFilterType`/`YearNr`) — svtf **20** leagues; the league finder tolerates
  `LoadMoreResults` on page 1 entirely.

So do NOT port the itfjuniors payload fix to the siblings on the basis of static
code similarity — they work as-is. The fixed payload is backward-compatible (also
returns results on the lenient hosts), but applying it is needless churn unless a
specific host later starts returning 0.

## The strict contract — enforced by `itfjuniors` (verified June 2026)

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

**Why:** the legacy ported payloads always sent `LoadMoreResults` and bare dates;
the `itfjuniors` host tightened its contract and began returning 0, but the
sibling hosts did NOT — so the bug is host-specific, not a shared-engine defect.
**How to apply:** if a TS-finder scraper reports 0, FIRST reproduce live against
that exact host (cold-session POST) — do NOT assume a code-similar sibling shares
the bug (proven false June 2026). Then check payload shape (LoadMoreResults/date
format) and confirm the window actually has data via a wide-range probe before
changing code.
