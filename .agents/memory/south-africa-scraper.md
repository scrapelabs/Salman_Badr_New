---
name: South Africa (SportyHQ) queue-driven scraper
description: The lone queue-driven MatchMiner scraper — works a queue of tournament keys instead of a date/year; its non-obvious API quirks and the reusable key-batch pattern.
---

# south_africa (Tennis South Africa / SportyHQ)

The only **queue-driven** scraper in the catalogue. Instead of a date/year input
it works through a queue of *tournament keys* (32-hex), each unlocking one
tournament's full result set from SportyHQ's public results API.

## The key-batch pattern (reusable shape)
A queue-driven scraper is wired by:
- `registry.INPUT_KEY_BATCH` input kind + `ScraperSpec.has_key_store=True`.
- A queue model (`SAKey`): one row per key, `status` pending/done/failed,
  `num_results`, `last_run`, `scraped_at`.
- The runner reads `run_obj.params`: `{"run_all": bool, "keys": [...]}`. `run_all`
  (always set by webhook/scheduler) drains the **entire** queue in ONE run — every
  key whose status != DONE (so it also retries FAILED), **no per-run key cap**;
  otherwise the pasted keys are upserted into the queue and processed. In BOTH
  modes a key already marked `done` is **skipped and logged** ("already processed —
  skipping") so it isn't re-scraped. `KEY_BATCH_MAX_KEYS` is now only a *paste*
  cap. Each processed key marks its row done/failed.
- **Empty work == SUCCESS no-op, not FAILED.** When nothing is left to process
  (queue drained / all pasted keys already done) the runner returns SUCCESS with 0
  rows. So post-drain scheduler/webhook fires create zero-row SUCCESS runs (by
  design, not a bug).
- UI: a **Key queue** tab gated on `has_key_store`, mirroring the
  Match-database (`data`) tab's plumbing (tab gate, paginated listing, nav-link
  `{% if %}`). Start form gets a textarea + "run the pending queue" checkbox.
- `validate_run_params` `INPUT_KEY_BATCH` branch: `run_all = webhook or checkbox`;
  else regex-extract `[0-9a-fA-F]{32}`, lowercase + dedupe, cap.

## Non-obvious API facts (don't relearn the hard way)
- **`X-API-KEY` is the site's PUBLIC results-feed key** — it travels in the
  page's own client-side requests. It rides in the request URL and is fine in
  the requests CSV; it is NOT a secret.
- **Per-result `tournament_key` is `null`.** The 64-col schema's **Key** column
  MUST be filled from the *input* key the run queried with, not the result field.
- Doubles iff both `user_3` AND `user_4` are present. `winner` (1|2) selects the
  winning side; team1=(u1,u2), team2=(u3,u4).
- Result-level `match_date` / tournament `start_date`/`end_date` are
  `YYYY-MM-DD` → output as `m/d/Y`.
- Player names arrive in mixed/ALL-CAPS casing → title-case only fully-uppercase
  tokens so `McCulloch` / `van der Merwe` survive.

## Caveat for future reuse
`SAKey.tournament_key` is **globally unique** and the runner's status updates
filter by key alone (not `(scraper, key)`). Fine while this model is
South-Africa-only. **Before adding a second key-store scraper**, make uniqueness
composite (`unique_together = (scraper, tournament_key)`) and scope the updates,
or two scrapers' queues will collide.

**Why:** the queue is single-tenant by construction today; the global-unique key
was the simplest correct choice for one scraper but silently breaks multi-scraper
reuse.

## Queue-driven scrapers have NO Real-time tab — run/monitor/stop from the Key queue
For `has_key_store` scrapers the Lab **hides the Real-time test tab**: the default
landing tab is `keys`, `?tab=real-time` 302-redirects to `?tab=keys`, the nav link
is `{% if not has_key_store %}`-gated, and other surfaces that linked to
`?tab=real-time` (overview recent-runs, scrapers-table "Open lab", Calls empty
state, Schedule copy) point to the keys tab or drop the param so the server's
default-tab logic routes per scraper. The Key queue tab carries its own launcher
("Run all pending keys (N)" → POST `run_all=on`), a maintenance/active/exclusivity
status note, **and** a Stop-run button (only when a run is active — it's the *only*
stop control now that the runbar is gone). Two sibling `<form>`s in a
`.rt-start-actions` flex row (can't nest forms). Monitoring is the **table itself**:
keys flip Pending→Done as they're scraped; the user refreshes (no live console).

**Why:** the real-time tab's ~1s live-console poll (`run_events`) hammered the
user's **remote/networked Postgres** (self-hosted Azure VM) and looked like the
scraper "failing"; the queue table already shows progress, so polling was pure
overhead for this scraper. The paste-keys textarea was dropped with it (queue is
run-all only) — re-addable as a secondary control if ad-hoc keys are ever needed.

## Row ceiling is soft
`KEY_BATCH_MAX_ROWS` is checked *between* keys, so the final key can push the
output slightly over the cap. Intentional — keep it unless a strict cap is
required.
