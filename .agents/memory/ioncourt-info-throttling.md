---
name: ioncourt /info endpoint throttling
description: Why ioncourt runs log curl-28 timeouts on tie-detail calls and why that's not a bug
---

# ioncourt `/api/tie/{id}/info` throttling

Ioncourt runs commonly emit a wave of `curl: (28) Operation timed out after 30000ms
with 0 bytes received` WARNs on the **phase-2 tie-detail** POSTs (`/api/tie/{id}/info`,
sent with `{"skipCache": false}`). This is **expected graceful degradation, not a bug.**

**What's actually happening**
- `login` and the `/api/search/ties` list respond instantly.
- Only the per-tie `/info` (and downstream `/tie-matches`) enrichment stalls: the server
  accepts the connection but returns **0 bytes** within the 30s timeout for *certain* ties.
- Pattern in real runs: a fast initial burst of successful ties, then sustained timeouts —
  consistent with server-side **rate-limiting / cache-miss slowness** keyed on the caller IP
  (cached ties come back fast; uncached ones hit a slow backend and never answer in time).

**Why:** reproduced **direct from the Replit datacenter IP (no proxy)** against the exact
tie IDs that timed out for the user — identical curl-28 timeouts. So it is **not** the
user's curl, **not** their proxy config specifically, and **not** a code defect. A
*datacenter* proxy (e.g. plainproxies.com) does **not** escape it — the throttle is on
datacenter traffic generally.

**How to apply / advise**
- The runner already retries each tie 4× then skips it, so the run still completes and
  writes every row it could fetch. Don't "fix" the timeouts — they're handled.
- To get more complete data: **re-run later** (slow/uncached ties warm up), **lower worker
  threads** (gentler → trips the throttle less), or try a **residential** proxy (same class
  of remedy as the Stadion / Davis Cup CloudFront block).
- Bumping the 30s per-request timeout rarely helps — 0-byte responses usually never arrive,
  and it just makes runs slower.
