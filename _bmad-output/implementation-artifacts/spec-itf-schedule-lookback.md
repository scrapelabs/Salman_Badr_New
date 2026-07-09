---
title: 'ITF schedule lookback days'
type: 'feature'
created: '2026-07-09'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'bd00a39b21844891b32a0cf1930b13e592f6b5a5'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The in-app Schedule tab lets operators choose Daily frequency while still leaving Day of week / Day of month controls interactive, which is confusing because those fields are ignored for daily runs. All five ITF scheduled cron jobs (`itf_juniors_tournament_software` plus the four `itftennis_*` circuits) also need an operator-selectable trailing date window instead of relying on the implicit generic rolling-window default.

**Approach:** Add an ITF-only schedule lookback-days dropdown with choices 5 through 45 days in 5-day steps, defaulting to 15 days, persist it on `ScraperSchedule`, and pass it into scheduled ITF runs as the existing `bi_weekly_days` rolling-window input. Update the Schedule tab JavaScript to disable irrelevant day selectors whenever Daily is selected.

## Boundaries & Constraints

**Always:** Preserve existing manual Real-time behavior; preserve existing non-ITF schedule behavior; keep cron jobs using the shared `validate_run_params` / `_enqueue_run` path; default ITF scheduled lookback to 15 days; restrict UI choices to 5, 10, 15, 20, 25, 30, 35, 40, and 45 days.

**Ask First:** If applying the lookback to rolling-window scrapers outside the five ITF slugs becomes necessary; if the range needs values outside 5..45; if the schedule cadence semantics need changing beyond disabling ignored selectors.

**Never:** Do not change ITF scraper network/discovery code; do not apply this setting to manual URL runs; do not introduce external cron dependencies; do not store secrets or hardcode deployment-specific URLs.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Daily schedule UI | Schedule tab frequency is `daily` | Day of week and Day of month selects are hidden/disabled so stale values cannot be edited for daily runs | Server still safely ignores/clamps stale posted weekday/month-day values |
| Weekly/biweekly/monthly UI | Frequency changes from `daily` to `weekly`, `biweekly`, or `monthly` | Relevant day selector is re-enabled and displayed; irrelevant selector stays disabled | No page reload required |
| ITF default | New or existing `itf_juniors_tournament_software`, `itftennis_womens`, `itftennis_mens`, `itftennis_masters`, or `itftennis_juniors` schedule has no explicit setting | Schedule page shows 15 days selected; scheduled run uses today minus 15 days through today | DB default supplies 15 for old rows after migration |
| ITF custom lookback | Operator saves 30 days for an ITF schedule | Schedule row persists 30; next cron-created run has `date_from=today-30`, `date_to=today`, and `params["bi_weekly"] == 30` | Invalid POST values fall back to 15 rather than widening unexpectedly |
| Non-ITF schedule | Operator opens/saves any non-`itftennis_*` schedule | No ITF lookback dropdown is shown; scheduler still uses existing defaults | Posted ITF-only field is ignored |

</frozen-after-approval>

## Code Map

- `artifacts/matchminer/accounts/models.py` -- `ScraperSchedule` persistence model; add the ITF lookback field and constants.
- `artifacts/matchminer/accounts/migrations/0051_scraperschedule_itf_lookback_days.py` -- schema migration for the new persisted field.
- `artifacts/matchminer/accounts/views.py` -- schedule POST parsing and GET context for ITF-only dropdown/options.
- `artifacts/matchminer/accounts/scheduler.py` -- due-schedule launcher; pass configured ITF lookback into `validate_run_params` for cron-created ITF runs.
- `artifacts/matchminer/templates/scraper_detail.html` -- Schedule tab form and JavaScript day-selector disabled state.
- `artifacts/matchminer/accounts/tests/test_scheduler.py` -- focused tests for ITF lookback persistence and scheduled run params.

## Tasks & Acceptance

**Execution:**
- [x] `artifacts/matchminer/accounts/tests/test_scheduler.py` -- add failing tests for ITF dropdown rendering/persistence, scheduler lookback application, and non-ITF omission -- locks requested behavior before code changes.
- [x] `artifacts/matchminer/accounts/models.py` and migration `0051_*` -- add `ScraperSchedule.itf_lookback_days` defaulted to 15 with valid-choice constants -- gives cron a durable per-schedule setting.
- [x] `artifacts/matchminer/accounts/views.py` -- expose ITF schedule context and parse the dropdown only for ITF slugs -- prevents non-ITF behavior changes.
- [x] `artifacts/matchminer/accounts/scheduler.py` -- pass `bi_weekly_days` for ITF schedules before enqueue -- reuses existing rolling-window validation and persists correct `Run.date_from/date_to/params`.
- [x] `artifacts/matchminer/templates/scraper_detail.html` -- add ITF lookback dropdown and disable hidden day selectors in JS -- matches operator UX request.

**Acceptance Criteria:**
- Given an ITF Schedule tab, when the page renders, then a lookback dropdown appears with 5-day increments through 45 and 15 selected by default.
- Given an operator saves 30 days on an ITF schedule, when the form posts, then `ScraperSchedule.itf_lookback_days` stores 30.
- Given an ITF schedule fires, when the scheduler creates the run, then the run covers today minus the selected number of days through today and `params["bi_weekly"]` equals that number.
- Given a non-ITF Schedule tab, when it renders or posts, then no lookback dropdown is shown and scheduler behavior is unchanged.
- Given Daily frequency is selected, when the Schedule tab syncs controls, then Day of week and Day of month controls are disabled and hidden.

## Spec Change Log

## Verification

**Commands:**
- `python manage.py test accounts.tests.test_scheduler --keepdb` -- expected: new targeted schedule tests pass.
- `python manage.py test --keepdb` -- expected: full Django test suite passes.

## Suggested Review Order

**Scheduler data flow**

- Start where cron injects the selected ITF rolling window.
  [`scheduler.py:234`](../../artifacts/matchminer/accounts/scheduler.py#L234)

- Confirm existing validator still owns date math and run params.
  [`views.py:1938`](../../artifacts/matchminer/accounts/views.py#L1938)

**Persistence and validation**

- Review all five ITF slugs, choices, defaults, and invalid-value fallback.
  [`models.py:756`](../../artifacts/matchminer/accounts/models.py#L756)

- Check the schema migration matches model choices and default.
  [`0051_scraperschedule_itf_lookback_days.py:8`](../../artifacts/matchminer/accounts/migrations/0051_scraperschedule_itf_lookback_days.py#L8)

**Schedule form behavior**

- Verify POST persistence is gated to only `itftennis_*` schedules.
  [`views.py:1219`](../../artifacts/matchminer/accounts/views.py#L1219)

- Verify schedule-page context exposes ITF-only dropdown metadata.
  [`views.py:1493`](../../artifacts/matchminer/accounts/views.py#L1493)

- Inspect the dropdown operators will use for 5–45 day choices.
  [`scraper_detail.html:1360`](../../artifacts/matchminer/templates/scraper_detail.html#L1360)

- Confirm hidden day selectors are also disabled during JS sync.
  [`scraper_detail.html:1440`](../../artifacts/matchminer/templates/scraper_detail.html#L1440)

**Regression tests**

- Review ITF dropdown default/options and POST persistence coverage.
  [`test_scheduler.py:65`](../../artifacts/matchminer/accounts/tests/test_scheduler.py#L65)

- Review invalid fallback and scheduler-created run coverage.
  [`test_scheduler.py:102`](../../artifacts/matchminer/accounts/tests/test_scheduler.py#L102)

- Review non-ITF omission and daily disabled-control coverage.
  [`test_scheduler.py:140`](../../artifacts/matchminer/accounts/tests/test_scheduler.py#L140)
