---
title: 'Fix QA ticket 767cfc55 Tennis Europe team events'
type: 'bugfix'
created: '2026-07-09'
status: 'draft'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** QA ticket `767cfc55-6873-4a47-906e-ab40b80e8a46` reports that the Tennis Europe date-range scraper misses team events. Evidence: date-range runs `#a713c9d8` and `#52478aeb` succeeded but their CSVs contain no `teammatch.aspx?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=18`, while direct URL run `#a5a1339b` against that same team-match URL succeeded with 3 rows.

**Approach:** Keep existing individual tournament scraping intact, and add an opt-in Tennis Europe path that discovers team-match links from discovered/supplied tournament pages and parses them through the already-working `_parse_team_match_page` pipeline. Prove the fix with failing-first scraper regression tests before changing production code.

## Boundaries & Constraints

**Always:** Preserve direct `teammatch.aspx` support; preserve individual entrant/player-profile scraping; dedupe team rows through the same content key as other rows; apply requested date-window filtering to discovered team-match rows when row dates are available; keep the change opt-in for Tennis Europe unless tests prove a shared TournamentSoftware behavior is safe.

**Ask First:** Changing QA ticket status, running large live production scrape jobs, enabling this behavior for every TournamentSoftware wrapper, or altering CSV schema/column names.

**Never:** Do not hardcode the ticket URL or match id as a special case; do not store secrets or proxy credentials in tests/specs; do not modify unrelated `.opencode` changes; do not replace the Tennis Europe scraper with a league scraper rewrite.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Date range includes a Tennis Europe team event | Discovered tournament page or draw page links to `/sport/teammatch.aspx?id=...&match=18` | Run enumerates the team-match URL, parses rows via `_parse_team_match_page`, and includes them in the CSV alongside individual rows | A bad team-match page records telemetry/log warning and does not fail the whole run |
| Direct team-match URL | `params.tournament_url` is a `/sport/teammatch.aspx` URL | Existing direct team-match path still parses that one page exactly once | Existing parse failure behavior remains unchanged |
| Individual-only tournament | Tournament page has no team-match links | Existing entrant listing and player-match scraping output is unchanged | Missing/failed team-link discovery returns an empty list and continues |
| Duplicate team-match links | Same team-match URL appears on tournament and draw pages | Link enumeration and row writing avoid duplicate output rows | Duplicate links do not inflate `progress_total` or CSV rows |

</frozen-after-approval>

## Code Map

- `artifacts/matchminer/accounts/live_scrapers/tennis_europe.py` -- Tennis Europe wrapper where the opt-in config flag should be enabled.
- `artifacts/matchminer/accounts/live_scrapers/_ts_tournament.py` -- Shared TournamentSoftware tournament engine; current date-range path discovers tournaments, lists entrants via `Players/GetPlayersContent`, and only handles team-match pages for direct URLs.
- `artifacts/matchminer/accounts/live_scrapers/_ts_league.py` -- Existing reference for enumerating team-match links before parsing individual match pages.
- `artifacts/matchminer/accounts/tests/test_ts_tournament.py` -- Existing regression tests for date windows, gender behavior, and direct team-match parsing; add the new failing-first coverage here.
- `artifacts/matchminer/accounts/models.py` -- `Run` fields used by integration-style tests (`csv_data`, `row_count`, status/progress updates).

## Tasks & Acceptance

**Execution:**
- [ ] `artifacts/matchminer/accounts/tests/test_ts_tournament.py` -- Add failing-first coverage for opt-in date-range/single-tournament team-match discovery and run integration -- Ensures the ticket failure cannot recur silently.
- [ ] `artifacts/matchminer/accounts/live_scrapers/_ts_tournament.py` -- Add a default-off config flag and helper(s) that enumerate team-match URLs from tournament/draw pages, dedupe links, and return `(match_url, ctx)` work items -- Keeps behavior narrow and reusable.
- [ ] `artifacts/matchminer/accounts/live_scrapers/_ts_tournament.py` -- Integrate discovered team-match work into the non-direct run path, parse via `_parse_team_match_page`, share Claude/DOB context, update progress totals, and apply date-window filtering where applicable -- Fixes the actual missing rows without bypassing existing parsing.
- [ ] `artifacts/matchminer/accounts/live_scrapers/tennis_europe.py` -- Enable the opt-in flag for Tennis Europe only -- Limits blast radius to the reported scraper.

**Acceptance Criteria:**
- Given a Tennis Europe date-range run discovers a tournament/draw page containing `teammatch.aspx` links, when the scraper runs, then matching team-match rows are written to the items CSV.
- Given the ticket’s team-match URL is supplied directly, when the scraper runs, then the existing direct team-match parser still returns rows and does not double-enumerate.
- Given an individual-only Tennis Europe tournament has no team-match links, when the scraper runs, then the existing entrant/player-profile crawl still produces the same rows it did before.
- Given duplicate team-match links are found, when enumeration completes, then each URL is scraped at most once.

## Spec Change Log

## Design Notes

Root cause found before fix: `_ts_tournament.run()` branches direct `/sport/teammatch.aspx` URLs to `crawl_team_match()`, but date-range/single-tournament paths only execute `list_one()` → `_list_players()` → `_parse_player_matches()`. Tennis Europe team events expose completed rubbers through `teammatch.aspx` pages, so a successful date-range run can still omit team events even though the direct parser works.

Use a narrow flag such as `discover_team_matches` rather than broadening all TournamentSoftware tournament wrappers. The helper should be conservative: collect direct `teammatch.aspx` anchors from the tournament page, collect legacy `draw.aspx` anchors and inspect those pages for team-match anchors, normalize with `urljoin`, and dedupe in discovery order.

## Verification

**Commands:**
- `python manage.py test accounts.tests.test_ts_tournament --keepdb` -- expected: targeted TournamentSoftware tests pass, including new red/green team-event regression.
- `python manage.py test --keepdb` -- expected: full Django test suite passes.
