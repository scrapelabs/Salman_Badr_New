---
name: Per-scraper job queue & concurrency gate
description: How the Batch-jobs queue admits/promotes runs and why the hysteresis gate lives in a DB singleton, not a module global.
---

# Per-scraper job queue & concurrency

All run creation (Run-now, scheduled, webhook) UNIFIES onto enqueue (QUEUED row)
+ `_dispatch_next()`. Only `scrape_now` CLI stays immediate. Never create a
RUNNING row directly for the queued paths — that bypasses the rules below.

## Admission rules (must stay in lockstep with the UI notices)

- One job per scraper at a time; extras queue, FIFO by `created_at`.
- Browser scrapers (`spec.uses_browser`): strictly ONE system-wide; while one
  runs, everything else queues. FIFO-strict: a head-of-queue browser job that
  can't start yet BLOCKS the scan (don't promote later request jobs past it) so
  it can't be starved.
- Request scrapers: concurrent under a GLOBAL thread budget =
  sum of `Scraper.worker_count` of running non-browser jobs, with hysteresis
  `REQUEST_THREAD_RESUME_LOW=10` / `REQUEST_THREAD_CAP_HIGH=30`.

`_dispatch_next()` serializes ALL workers via a Postgres advisory **xact** lock
(`RUN_START_LOCK_KEY`) inside `transaction.atomic()`. Poll pumps call it
non-blocking (`pg_try_advisory_xact_lock`) and defer if another holds it.

## Why the hysteresis gate is a DB singleton, not a module global

**Why:** prod runs gunicorn with multiple workers. A module-global gate diverges
per process — the hard HIGH cap still holds (it's recomputed from fresh DB state
every pass) but the LOW/HIGH "drain before re-admitting" band would not, so one
worker could re-admit while another thinks the gate is closed.

**How to apply:** the gate lives in `QueueState` (pk=1 singleton,
`request_gate_open`). Read/write it ONLY inside `_dispatch_next()`'s
advisory-locked txn — that lock is the serialization primitive, so no
`select_for_update` is needed. Persist only when the flag actually flips
(`save(update_fields=["request_gate_open","updated_at"])`) to avoid write churn.
If you ever add admin/manual editing of `QueueState`, take the same lock.

**Caveat:** on a live migration with request jobs already mid-band (11-29
threads) the singleton defaults open and can't reconstruct a previously-closed
in-memory gate. Hard HIGH cap still enforced; only exact hysteresis continuity
across that one deploy is lost. Seed `QueueState` during maintenance if it
matters.

## Cancelling a QUEUED job

Must be an atomic conditional UPDATE
(`Run.objects.filter(uuid=..., status=QUEUED).update(...)` + rowcount check),
never read-then-save — otherwise a job promoted to RUNNING between the read and
the write gets wrongly cancelled.
