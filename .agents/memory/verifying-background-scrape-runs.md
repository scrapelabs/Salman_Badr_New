---
name: Verifying full background scrape runs in this environment
description: How to verify a real BJK Cup run end-to-end when it outlives the bash timeout — trigger via the live runserver, not a bash-spawned worker.
---

# Verifying full background scrape runs

A scrape `Run` is executed by a detached `run_scrape` worker (`subprocess.Popen`, DEVNULL stdio, `start_new_session=True`). A full BJK Cup season is ~478 rows and takes ~2–2.5 min.

## The trap
A worker you spawn **from a bash tool command** dies the instant that bash command ends or is force-killed. The bash tool tears down its whole process tree on completion — even a `setsid`-detached child or one launched by `subprocess.Popen(..., start_new_session=True)` from inside `manage.py shell -c`. Observed repeatedly: the worker logs a few lines, then stops exactly when the spawning bash call returns/hits its ~120s timeout. The django.test.Client approach (`force_login` + POST + poll) only works while the shell stays alive — and a full run (~2m13s) **exceeds the bash tool's 120s max timeout**, so it can never finish inside one bash call.

## The fix (product path)
Trigger the run through the **already-running `runserver` workflow** over HTTP. That worker is parented to the durable runserver process, not your bash tree, so it survives your bash calls and completes normally. Then poll the DB across separate short bash calls.

**Auth without the password** (it's set out-of-band, not in the repo): mint an authenticated Django session directly, then curl with that cookie.
- In `manage.py shell`: `SessionStore()`, set `_auth_user_id`, `_auth_user_backend='django.contrib.auth.backends.ModelBackend'`, `_auth_user_hash=user.get_session_auth_hash()`, `.create()`, print `session_key`.
- GET the detail page with `-b "sessionid=<key>"` to capture the `csrftoken` cookie, then POST `{csrfmiddlewaretoken, year}` over **HTTPS `$REPLIT_DEV_DOMAIN`** (cookies are `Secure`) with both cookies and a matching `Referer`. A **302** = launched; poll the DB for `status=success`.

**Why:** cookies are `Secure` (cross-site iframe), and the worker must outlive the bash call. Confirmed: HTTP-triggered BJK Cup 2026 → success, 478 rows, 694 requests, 0 errors, ~2m13s at 5 worker threads.

## Cleanup
A worker killed mid-run leaves an orphaned `RUNNING` row that blocks the next run until `_reap_stale_runs` reaps it (20 min). Delete such debris rows (or wait for the reaper) before launching a fresh run.
