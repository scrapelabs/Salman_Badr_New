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

## south_africa has NO Key queue tab — run/monitor the queue from the Real-time tab
The Lab's **Key queue tab is retired from the UI**. `?tab=keys` 302-redirects to
`?tab=real-time` (unconditionally — only south_africa ever had the tab), the nav
link is removed, and the seeded `Scraper.description` no longer points at it (fixed
in the seed migration **and** via a follow-up data migration so existing
self-hosted DBs get the corrected copy on `migrate`). The queue is still launched +
monitored from the **Real-time tab**, unchanged: its start form (paste-keys
textarea + "run the whole pending queue" checkbox) + live console still work, and
keys still flip Pending→Done in the DB as they're scraped. The `elif tab == "keys"`
view ctx branch and the `{% elif tab == 'keys' %}` template block are left in place
but are now **unreachable dead code** (the redirect fires first) — kept for easy
re-enable.

**Why:** the user explicitly asked to remove the Key queue tab and keep Real-time.
An earlier attempt did the opposite (hid Real-time, made `keys` the default) and was
reverted. The queue *table* was the only thing the tab added; everything needed to
run and watch the scraper already lives on the Real-time tab.

## Row ceiling is soft
`KEY_BATCH_MAX_ROWS` is checked *between* keys, so the final key can push the
output slightly over the cap. Intentional — keep it unless a strict cap is
required.
