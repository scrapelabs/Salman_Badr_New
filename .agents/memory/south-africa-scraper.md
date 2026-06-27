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
  (always set by webhook/scheduler) drains the **pending** queue capped at
  `KEY_BATCH_MAX_KEYS`; otherwise the pasted keys are upserted into the queue and
  processed. Each key marks its row done/failed.
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

## Row ceiling is soft
`KEY_BATCH_MAX_ROWS` is checked *between* keys, so the final key can push the
output slightly over the cap. Intentional — keep it unless a strict cap is
required.
