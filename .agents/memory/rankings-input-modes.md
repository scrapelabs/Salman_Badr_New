---
name: Rankings scraper input modes
description: ATP collects a date-RANGE of weekly snapshots; WTA stays single-snapshot. Changing a scraper's input_kind needs NO migration.
---

# Rankings scraper input modes

The player-ranking scrapers deliberately differ in how a run is parameterised:

- `atptour` → `INPUT_DATE_RANGE`: one run collects **every weekly ranking
  snapshot** (ATP publishes each **Monday**) inside `[date_from, date_to]`
  inclusive. `_rankings.snapshot_dates()` enumerates the Mondays (first Monday
  on/after `date_from`, then +7 through `date_to`).
- `wtatennis` → `INPUT_RANK_SNAPSHOT`: a single snapshot date. Left unchanged on
  purpose — the range request was ATP-only.

**Why the asymmetry:** the user asked to "enable range for ATP" only. Don't
reflexively make WTA a range too (or ATP single again) without a fresh request.

**How `snapshot_dates` stays backward-compatible:** it returns `[single_date]`
first when that param is present (so historical rank-snapshot runs still work),
then the Monday enumeration, then a `[start]` fallback for a sub-week window
containing no Monday (intentional "don't silently collect nothing").

## The reusable lesson: input_kind is registry-driven

A scraper's input mode lives in the `LIVE_SCRAPERS` registry spec
(`spec_for(slug).input_kind`), **not** in a DB column. So flipping a scraper
between snapshot / date-range / etc. is a **code-only** change — **no Django
migration, no `Scraper` model change**. The date-range form branch, validation
(`validate_run_params`), and both Schedule-tab YAML generators key off the same
registry spec, so they pick up the new mode automatically.

**Multi-week row identity:** `_discover` stamps each player with its own
`range_date`/`rankdate` and only dedups within a single call, so the same player
recurs once per week → one row per player per week (correct, not a dup bug).
A thread-safe `bio_cache` reuses each player's static hero bio across weeks
(not strict single-flight; a rare concurrent miss double-fetches — harmless).
