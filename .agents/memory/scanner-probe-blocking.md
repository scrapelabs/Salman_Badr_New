---
name: Scanner-probe blocking middleware & Django >=400 logging
description: How MatchMiner silently drops bot/vuln-scanner probes, and the non-obvious Django rule that returning a 404 still logs a warning.
---

# Blocking scanner probes quietly

Public MatchMiner servers get constant bot traffic probing for `/.env`,
`*.php`, `/wp-admin/...`, phpunit RCE paths, etc. `BlockProbesMiddleware`
(`accounts/middleware.py`, registered **first** in `MIDDLEWARE`) matches these
with a narrow regex and returns a bare `HttpResponseNotFound()` before routing,
CSRF, or static serving run.

## The non-obvious part: returning a 404 is NOT enough to silence the log
Django's `BaseHandler.get_response()` logs **every** response with
`status_code >= 400` via `django.utils.log.log_response()` — the
`Not Found: <path>` warning on the `django.request` logger. This fires for a
404 you *return from middleware*, not just for a raised `Http404`. So a probe
that's cleanly short-circuited still spams the log.

**Fix:** set `response._has_been_logged = True` on the response you return.
`log_response()` checks that exact flag and skips — it's the same flag Django
sets internally to avoid double-logging. With it, the probe drop is fully
silent (on Waitress, which logs no per-request access line, the probe noise
disappears entirely; under `runserver` you still see the dev access-log line,
which is separate from the warning).

**Why:** the user asked to stop scanner-probe log spam; just 404ing left the
warnings because the base handler logs by final status, not by code path.

## Regex must stay precise
The app vendors assets under `/static/vendor/quill/...`, so do NOT block a
generic `vendor/` segment — the `.env`/`.php`/`phpunit` rules already catch the
`/vendor/.../.env` probes without touching legit `/static/vendor/...`. Verify
after edits: probes → 404, `/`, `/static/favicon.svg`, and
`/static/vendor/quill/quill.js` → 200.

## Out of scope (left as-is)
The `Forbidden (CSRF cookie not set.): /` warnings are bots POSTing the login
form without a CSRF token — that's CSRF protection working. Not suppressed,
because globally silencing `django.security.csrf` could hide real issues.
