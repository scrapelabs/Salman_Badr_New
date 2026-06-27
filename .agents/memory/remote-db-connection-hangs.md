---
name: Remote DB persistent-connection hangs
description: Why pages "take a while then finish but mostly don't" on a self-hosted server talking to a networked Postgres, and the settings fix.
---

# Remote/networked Postgres + persistent connections = stale-socket hangs

**Symptom:** A page (e.g. the south_africa Key queue tab) loads instantly on the
Replit preview but on the user's own server (e.g. an Azure VM on plain HTTP)
"takes a while then finishes, but most of the time doesn't finish" — even though
it only loads ~50 paginated rows. The user blames the table/query; it is NOT the
query.

**Root cause:** `conn_max_age > 0` (we use 600) keeps a Postgres connection open
across requests. When the DB is across a network (not the local Replit DB),
firewalls / NAT gateways / DB proxies silently drop idle TCP connections. With no
health check, Django grabs the dead socket for the next request and blocks until
the OS TCP timeout (often minutes) before erroring/reconnecting — hence
"sometimes finishes, mostly hangs". The Replit preview never shows this because
its DB is local/low-latency and rarely drops idle sockets.

**Fix (in `matchminer/settings.py`, on the postgres ENGINE):**
- `CONN_HEALTH_CHECKS = True` — Django validates a reused connection once per
  request and reconnects if dead. Cheap vs. minute-scale hangs.
- libpq `OPTIONS` via `setdefault` (so the user's own `DATABASE_URL` query opts
  like `sslmode` still win): `connect_timeout=10`, `keepalives=1`,
  `keepalives_idle=30`, `keepalives_interval=10`, `keepalives_count=5`.
- `conn_max_age` is env-overridable via `DJANGO_DB_CONN_MAX_AGE` (default 600);
  set it to `0` on a flaky remote DB to force a fresh connection per request as a
  diagnostic / last resort.

**Why:** persistent connections are a latency win but a correctness hazard on
unreliable networks; health checks + keepalives turn a multi-minute hang into a
fast reconnect.

**How to apply / next steps if hangs persist after restart:** old sockets are
only discarded on process restart, so restart the server first. If it still
hangs, suspect connection-pool exhaustion (Waitress threads + scheduler thread +
scrape-worker subprocess vs. a low Postgres `max_connections`) or lock/wait
contention — check `pg_stat_activity` during a hang for connection count, wait
events, and idle-in-transaction sessions.

**Also reduced query count** on that tab: the four per-status `COUNT`s were
collapsed into one `aggregate(Count(..., filter=Q(...)))` — each saved query is a
saved network round-trip on a remote DB.
