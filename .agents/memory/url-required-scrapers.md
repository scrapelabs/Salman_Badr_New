---
name: url_required scrapers (lockstep across input surfaces)
description: A scraper whose input_kind has no date-only mode (e.g. college_dual_match) must mirror that requirement across every input surface, or the docs hand the user a payload that 400s.
---

Some scrapers (input_kind `date_range_or_url` with `url_required=True`, e.g. college_dual_match — needs a tournament / box-score / Google-Sheet URL) have **no date-only fallback**: an empty submit honest-fails.

**Rule:** the `url_required` (and more generally each `input_kind`) contract must be mirrored in lockstep across FOUR places, or one surface contradicts the others:
1. the Real-time-test **start form** (template) — render the URL field `required`, hide From/To.
2. `validate_run_params` — raise a 400 when `url_required` and no URL (the URL branch runs first, the guard before any date fallback; the guard must also override the webhook empty-input fallback).
3. the Schedule-tab **example curl JSON** generator (`_trigger_example_json`).
4. the Schedule-tab **GitHub Actions workflow YAML** generator (`_github_workflow_yaml`).

**Why:** if (3)/(4) still emit `date_from`/`date_to` defaults for a `url_required` scraper, the copy-ready schedule docs hand the user a payload that the validator rejects with 400 — the docs would actively mislead. The curl JSON and the YAML payload must agree with each other and with the validator.

**How to apply:** when adding a new `input_kind` or flipping `url_required`, grep for every consumer of `spec.input_kind` / `spec.url_required` (start form, validator, both schedule generators, the run-complete JS that re-enables inputs) and update them together. The completion JS should re-enable ALL `.rt-start .rt-input` controls, not one specific field, so it stays correct as the form's inputs change per input_kind.
