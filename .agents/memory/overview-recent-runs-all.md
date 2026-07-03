---
name: Overview recent-runs feeds all runs
description: Why the Overview "recently active" live table sends every run, not a capped slice.
---

The Overview "recently active" table is driven by `_recent_runs()` and refreshed
every ~3s by the `live_stats` poll (the json_script page seed and the poll both use
the same helper). It returns ALL runs except QUEUED, newest first by `started_at`;
the client paginates 5 per page so page 1 is always the latest.

**Why:** the user explicitly asked to "add all run jobs" and have page 1 be the
latest. A previous version capped it at running + 5 finished, which looked like "no
pagination" and hid run history. Queued jobs are excluded because they have their
own panel above the table.

**How to apply:** don't reintroduce a finished/row cap in `_recent_runs()` to
"optimize" the poll without asking — that silently drops history the user wanted.
If run volume grows large enough that the 3s poll payload hurts, switch this table
to real server-side pagination (like the Users page) rather than truncating.
