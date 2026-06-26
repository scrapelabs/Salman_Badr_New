---
name: In-app recurring scheduler
description: How MatchMiner's GitHub-free recurring scraper scheduler works and the non-obvious traps when changing or verifying it.
---

# In-app recurring scheduler (MatchMiner)

A single background **daemon thread** (`accounts/scheduler.py`) fires due
`ScraperSchedule` rows on their cadence (daily/weekly/biweekly/monthly + time +
weekday/day-of-month + IANA tz), with **no external cron**. Started from
`AccountsConfig.ready()`. Pure UTC date math lives in `accounts/scheduling.py`.

## Process-gating is mandatory — don't spawn the thread everywhere
`ready()` runs for **every** Django entrypoint, including `migrate`, the
`run_scrape` worker subprocess, `scrape_now`, `shell`, `check`. The thread must
only start in a *web-serving* process.
**Why:** otherwise a fresh scheduler thread spawns inside the per-run worker
subprocess (and every mgmt command), causing duplicate fires / wasted threads.
**How to apply:** `should_run_in_this_process()` allows only `gunicorn` in argv,
or `runserver` with `--noreload` (single process) / `RUN_MAIN=="true"` (reloader
child). Anything else (mgmt commands) returns False. There's also an env
kill-switch `MATCHMINER_SCHEDULER_ENABLED=false`.

## Multi-worker safety = advisory-xact-lock + claim + advance-before-launch
Prod gunicorn has several workers; each starts its own thread.
**Why:** without coordination, N workers would each fire the same due row.
**How to apply:** each `tick()` opens a txn, grabs `pg_try_advisory_xact_lock`
(key `0x6D6D7363`); losers return immediately. The winner `select_for_update()`s
due rows ordered by `next_run_at`, **advances `next_run_at`/`last_fired_at`
before** launching, commits, then launches outside the lock. Launch reuses the
same `validate_run_params(...,webhook=True)` + `_start_scraper_run(launched_by=
None)` path as the button/webhook, so maintenance / single-in-flight /
browser-exclusivity guards all still apply. `_launch()` re-checks `enabled` so a
disable between claim and launch is honoured immediately.

## Policy: at-most-once, NO backfill
A schedule missed while the app was offline fires once on recovery (because
`next_run_at` is in the past), then resumes its normal cadence — it does **not**
replay every missed slot. `tick()` advances from `now`, not from the old due
instant. Intentional; document it, don't "fix" it.

## Verifying the live thread is actually running (the hard part)
CPython **3.11 does not propagate `Thread(name=...)` to the OS** `comm`, so
`/proc/<pid>/task/*/comm` just shows `python` — you can't detect the thread by
name (this changed in 3.12). Also, `accounts.scheduler` logs at INFO and
propagate to root, which Django's **default** logging config does NOT print to
console — so absence of the "scheduler started" line is **not** proof it failed.
**How to apply (gold-standard probe):** set a scraper to **maintenance**, enable
its schedule with `next_run_at = now - 2min`, wait one tick (~50s, TICK=45s), then
confirm the *live* process advanced `next_run_at` + set `last_fired_at` while
spawning **zero** runs (maintenance → `RunStartError` → `_launch` skip, no
worker). Reset `last_fired_at=None` before the probe so the change is unambiguous.
For pure math, unit-test `scheduling.compute_next_run` directly (no DB).

## Restart after changes
`runserver --noreload` + the long-lived thread mean the running scheduler holds
**old code** until you restart the `artifacts/permitlify: web` workflow. Always
restart after editing `scheduler.py` / `scheduling.py` / settings.
