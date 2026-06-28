---
name: Remote Postgres mid-request connection drops
description: Why CONN_HEALTH_CHECKS isn't enough for a flaky networked DB, and the GET-only retry middleware that fixes it.
---

On a self-hosted box talking to a REMOTE/networked Postgres (e.g. the user's Azure VM over the internet), `OperationalError: server closed the connection unexpectedly` can fire **mid-request** — a query deep in template render dies even though the session/auth middleware queries earlier in the SAME request succeeded. That proves the socket was alive at request start and died during the request.

**Why the existing settings fix is insufficient:** `CONN_HEALTH_CHECKS` + libpq keepalives + `connect_timeout` (already in `settings.py`) only validate a pooled connection at the **start** of a request (on its first use). They cannot catch a socket the server/NAT/DB-proxy drops **between** queries within one request. So diagnosing "but we already added health checks" is a trap — that covers stale-idle-at-request-start, a different failure.

**The fix:** `accounts.middleware.DBReconnectMiddleware`, registered 2nd in `MIDDLEWARE` (just inside `BlockProbesMiddleware`, so its retry wraps session/auth middleware + the view + template render). It catches `OperationalError`/`InterfaceError`, and **only** for idempotent methods (GET/HEAD) whose message matches a dropped-connection signature, force-closes all connections (`connections.all()` → `conn.close()`; thread-local under Waitress) and replays the request **once**. POST/writes, unrelated DB errors, and a persistent double-failure all re-raise untouched.

**Why:** a one-shot retry of a read-only page load is far safer than a recurring 500; writes must never be auto-retried (double-submit). Safe here because there is **no `ATOMIC_REQUESTS`** — every statement autocommits, so there's no half-finished transaction on retry.

**How to apply / constraints:**
- Any GET endpoint that relies on this global retry MUST stay idempotent (this app's GET side-effects — `_reap_stale_runs`, schedule `get_or_create`, notification mark-read — are benign/idempotent, so a rare retry is fine).
- If the VM stays flaky, `DJANGO_DB_CONN_MAX_AGE=0` (env knob already supported) reduces stale pooled sockets as a conservative complement.
- Watch the `accounts.security` warning log for retry frequency; frequent retries mean the remote DB/network itself needs operational remediation, not more app-side patching.

**Tangent that surfaced this:** `/scrapers/south_africa/` 500'd on the **keys tab** paginator. On older commits the keys tab was the DEFAULT for queue-driven scrapers, so a no-`?tab=` GET ran a `SAKey` queryset pagination. The keys tab was later removed (`?tab=keys` → real-time), whose log replay is a `log_text`-snapshot **list** (no heavy query). So pulling latest also moves SA off the heaviest default query path — but the retry middleware is what actually makes any page survive the drop.
