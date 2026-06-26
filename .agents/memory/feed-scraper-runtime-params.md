---
name: Feed-scraper run-time params & daily-schedule blank-date contract
description: How feed scrapers expose api_key/gender as run-time params, and why scheduled date_range YAML must post a BLANK date window.
---

## Feed scrapers expose api_key + gender as run-time params (not just constants)

Some "feed" scrapers (e.g. `new_jersey_high_school`) read a feed **API key** and a
**gender** (boys/girls/both) at run time from `Run.params` (a JSONField — no migration
to add params). The `ScraperSpec` carries optional flags (`feed_api_key` /
`feed_api_key_default` / `feed_gender`) that drive: the start-form fields, validation,
and the Schedule-tab generators. The runner falls back to a settings/module key + both
genders when params are absent.

**Lockstep rule (same family as the url_required lockstep memory):** any feed param must
be wired in *all* of: registry spec flag → start form field → `validate_run_params`
(normalize + cap + label) → realtime context defaults → `sched_defaults` → BOTH schedule
generators (`_trigger_example_json` curl JSON **and** `_github_workflow_yaml`
inputs/env/`--data`). Miss one and the Schedule tab hands the user a payload the
validator rejects, or a field silently never reaches the worker.

**`feed_api_key` is NOT secret-grade.** It is visible in the start form, baked as a
default into the generated YAML/curl, persisted in `Run.params`, and echoed back in the
webhook success JSON. That's acceptable for a *public* feed key (the source hard-codes
it), but **never reuse this field for a real secret/token** — route real secrets through
a secret/password-style path instead and keep them out of params/YAML/webhook responses.

## Scheduled date_range runs must post a BLANK date window

**Rule:** the `date_range` (and `date_range_or_url` date branch) GitHub-Actions YAML
generator emits **blank** date defaults (`default: ""`, env `${{ ... || '' }}`), NOT a
baked date window.

**Why:** a cron job runs the *same* generated YAML every day. If the YAML baked in a
concrete `date_from`/`date_to`, every scheduled run would re-scrape the **same frozen
window** forever. The webhook already has a server-side fallback that computes a fresh
**trailing window ending "today"** — but only when BOTH `date_from` and `date_to` arrive
blank. Posting blank dates from cron therefore makes daily runs actually advance.

**How to apply:** keep the blank-date YAML emission and the webhook's both-dates-blank
trailing-window fallback in lockstep. Manual `workflow_dispatch` can still type explicit
dates; partial (one blank) still 400s on purpose. This blank-date emission applies to
**every** date_range scraper's generated YAML (a general correctness fix, not NJ-only) —
no date_range runner requires non-blank dates because the webhook materialises concrete
dates onto the Run before the worker starts.
