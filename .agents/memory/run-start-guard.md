---
name: MatchMiner run-start guard & browser exclusivity
description: The single guarded choke-point every run-creation path must use, and the browser-exclusivity rule. Read when adding any new way to start a scrape (CLI command, API endpoint, scheduler) or touching run-start logic.
---

# Run-start guard — one choke-point, browser exclusivity

All `Run` creation in MatchMiner goes through **one** helper:
`accounts.views._create_guarded_run(scraper, *, inputs, launched_by)`. It is the
only place that calls `Run.objects.create(...)` for a real start.

**Rule:** any new way to start a scrape (a new management command, an API
endpoint, a future scheduler) MUST call `_create_guarded_run`, never
`Run.objects.create` directly.
**Why:** the guard bundles four invariants that silently break if a path skips it
— maintenance gate, reap-all stale runs, single-in-flight-per-scraper, and
**browser exclusivity**. A direct create bypasses all of them. (This bit us once:
the `scrape_now` CLI created rows directly and bypassed browser exclusivity.)

## Browser exclusivity (the resource invariant)
Browser-based sources = the **itftennis family only** (`itftennis_juniors/masters/
mens/womens`), flagged by `ScraperSpec.uses_browser=True` in `registry.py`. Each
such run drives a pool of up to ~5 headless Chrome, so it needs the host to itself:
- starting a **browser** run is blocked if **ANY** other scraper has a RUNNING run;
- starting a **non-browser** run is blocked only if a **browser** run is RUNNING;
- two non-browser (curl) runs may still run concurrently.
Blocked starts raise `RunStartError("busy", …, 409)`.

## How the guard is built (don't regress these)
- `_reap_stale_runs()` is called with **no arg = reap ALL scrapers** (not just this
  one), so a crashed browser run elsewhere can't hold exclusivity forever.
- The in-flight + exclusivity checks + the create run inside
  `transaction.atomic()` guarded by `pg_advisory_xact_lock(RUN_START_LOCK_KEY)` so
  simultaneous starts (e.g. many cron webhooks at the same minute) serialize. The
  advisory **xact** lock auto-releases on commit — by which point the new RUNNING
  row is visible to the next contender. Postgres-only is fine (app is PG-only).
- `RunStartError` raised inside the atomic block rolls back and is **not** caught by
  the inner `except IntegrityError` (which only maps the partial-unique race →
  `already_running`/409). Callers translate: web → `messages.error`, webhook → JSON
  status, `scrape_now` → `CommandError(str(exc))`.
- `_create_guarded_run` does **not** launch. `_start_scraper_run` = guard + then
  `_launch_run` (detached subprocess). `scrape_now` = guard + then
  `call_command("run_scrape", uuid)` **in-process** (the lock is already released;
  the persisted RUNNING row is the long-lived exclusion signal for the scrape's
  whole duration — that's intended, not a leak).
