---
title: 'Fix QA ticket d327635b Ireland handicap exclusions'
type: 'bugfix'
created: '2026-07-09'
status: 'done'
review_loop_iteration: 0
baseline_commit: '99680a071c23ab0966ed18175f3dc88472ebedc7'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** QA ticket `d327635b-0cbc-4c81-a161-4d27c429346e` reports that Ireland Tournament should exclude events whose names contain `Handicaps`. Current evidence from run `#4deacfdc` shows Ireland output includes `Senior Handicaps 2026 MEMBERS ONLY`, `Senior Handicaps 2026_MLTC Dublin Members only`, and `St Mary's Club Handicap Championships 2026`.

**Approach:** Use the existing shared TournamentSoftware `exclude_name_terms` mechanism for Ireland Tournament, configured with the `handicap` stem so both `Handicap` and `Handicaps` are filtered case-insensitively. Prove it with a failing-first regression before production code changes, then run one controlled Ireland job after tests pass and restart Waitress at the end.

## Boundaries & Constraints

**Always:** Keep the change scoped to `ireland_tournament`; preserve the shared `_ts_tournament` filtering semantics; test normal Ireland tournament names are kept and handicap variants are skipped; run one live Ireland job only after automated tests pass; restart Waitress only after code/test verification and the live job attempt.

**Ask First:** Changing QA ticket status, broadening the exclusion to other countries, changing CSV schema, committing/pushing, or running a large multi-week/month Ireland scrape if a narrower job can verify the fix.

**Never:** Do not hardcode a specific tournament URL/run id from the ticket; do not remove unrelated Ireland gender fallback behavior; do not modify unrelated previous Tennis Europe commits; do not treat a live job as a replacement for automated regression coverage.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Ireland handicap tournament | Discovered Ireland tournament name contains `Handicap`, `Handicaps`, or mixed-case equivalent | Tournament is excluded before entrant listing/crawling | Log existing shared skipped-count message; continue other tournaments |
| Normal Ireland tournament | Discovered Ireland tournament name does not contain `handicap` | Tournament remains eligible for scraping | Existing behavior unchanged |
| Non-Ireland TournamentSoftware wrapper | Luxembourg/other wrappers with their own config | Existing exclusions remain unchanged | No cross-wrapper behavior change |

</frozen-after-approval>

## Code Map

- `artifacts/matchminer/accounts/live_scrapers/ireland_tournament.py` -- Ireland wrapper config where the exclusion should be enabled.
- `artifacts/matchminer/accounts/live_scrapers/_ts_tournament.py` -- Shared TournamentSoftware engine; already has `exclude_name_terms` and `_filter_tournaments()`.
- `artifacts/matchminer/accounts/tests/test_luxembourg_tournament.py` -- Existing pattern for wrapper-specific name exclusion tests.
- `artifacts/matchminer/accounts/tests/test_ireland_tournament.py` -- New/target Ireland regression test surface.
- `artifacts/matchminer/accounts/management/commands/scrape_now.py` -- Controlled one-job live verification command path.

## Tasks & Acceptance

**Execution:**
- [x] `artifacts/matchminer/accounts/tests/test_ireland_tournament.py` -- Add failing-first SimpleTestCase for Ireland handicap-name exclusion and normal-name retention -- Proves the ticket issue independently of live upstream state.
- [x] `artifacts/matchminer/accounts/live_scrapers/ireland_tournament.py` -- Add `exclude_name_terms=("handicap",)` to `CONFIG` -- Uses existing shared filter with Ireland-only scope.
- [x] Live verification -- After tests pass, run controlled Ireland Tournament jobs: direct known-handicap URL `94acabe7` logged `Skipped 1 excluded tournament(s)` with no handicap rows, and direct normal URL `1ce6a3b4` completed `success` with 28 rows and no handicap tournament names -- Confirms production path.
- [x] Production restart -- Restart Waitress after test/job verification and confirm local HTTP health -- Applies code safely.

**Acceptance Criteria:**
- Given Ireland discovery includes normal and handicap-named tournaments, when `_filter_tournaments()` runs with Ireland config, then only normal tournaments remain.
- Given a handicap tournament name is plural, singular, or mixed case, when Ireland filtering runs, then it is excluded.
- Given another TournamentSoftware wrapper is configured independently, when this change is applied, then its existing exclusion terms are unchanged.
- Given tests pass and one controlled Ireland job completes, when Waitress is restarted, then `http://127.0.0.1/` returns HTTP 200.

## Spec Change Log

## Design Notes

The shared filter already performs case-insensitive substring matching on configured terms and is called after date-range or single-tournament discovery, before entrant listing. Configuring the term as `handicap` intentionally covers both the ticket’s plural `Handicaps` and existing live output using singular `Handicap Championships`.

## Verification

**Commands:**
- `python manage.py test accounts.tests.test_ireland_tournament accounts.tests.test_luxembourg_tournament --keepdb` -- passed: 3 tests OK.
- `python manage.py test --keepdb` -- passed: 76 tests OK.
- `python manage.py scrape_now ireland_tournament --url "https://ti.tournamentsoftware.com/sport/tournament?id=8303DC9B-E1F2-42C4-B0C4-F6737BAD31F2"` -- observed: Run `94acabe7` logged `Skipped 1 excluded tournament(s)` and emitted no handicap rows; framework status was `failed` because the excluded direct tournament produced zero rows.
- `python manage.py scrape_now ireland_tournament --url "https://ti.tournamentsoftware.com/sport/tournament?id=4224D54A-6CE5-40CB-9C7E-8B791F30FCAE"` -- passed: Run `1ce6a3b4` status `success`, 28 rows, no tournament name containing `handicap`.
- Waitress restart + `Invoke-WebRequest http://127.0.0.1/` -- passed: HTTP 200, response length 7521.
