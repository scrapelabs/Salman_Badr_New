---
name: Dual-mode input toggle (date range OR tournament link)
description: How the "Run by: Date range / Tournament link" segmented toggle works for date_range_or_url scrapers, and the contract its JS depends on.
---

Dual-mode scrapers (`input_kind == date_range_or_url` AND `url_required=False`, e.g. croatia_league / denmark_league / the *_league set) render BOTH a tournament-URL field and From/To dates. A segmented radio toggle (`name="input_mode"`, values `dates` default / `url`) lets the user pick one; the JS shows the chosen group and clears+hides the other so only one is ever submitted.

**Backend is already URL-first** (`validate_run_params`: if `tournament_url` non-empty it returns that and sets dates=None before any date parse). So the toggle is **UI-only** — no validator change. It also degrades gracefully with JS off: empty URL → dates used; non-empty URL → URL wins.

**The JS contract (template class/attr names the toggle JS keys off):**
- toggle container: `[data-mode-toggle]` (only emitted when `allows_url and not url_required`)
- URL group wrapper: `.rt-url-group`; date field wrappers: `.rt-dates-group`
- date inputs carry `data-default-date` so switching back to dates mode restores the default window
Rename any of these in `partials/run_params_fields.html` → update the JS in `scraper_detail.html` in lockstep.

**Two non-obvious placement rules (both cost real debugging):**
1. The toggle JS must live OUTSIDE the tab `{% if/elif tab == … %}` chain (put it near `{% endblock %}`). The partial is included by BOTH the Batch form and the Real-time form, but only ONE tab's form renders per request; JS scoped inside one branch silently leaves the other tab's toggle dead.
2. Drive the toggle with show/hide (`.rt-hide`) + value-clearing ONLY — never the `disabled` attribute. The real-time poll JS and the run-complete JS both own `disabled` on `.rt-input`, and would fight a disabled-based toggle. Set `required` only on the VISIBLE url field (required on a display:none field throws "not focusable" on submit).

Verify by rendering `scraper_detail` with `django.test.Client` for one dual / one url_required / one pure-date scraper on `?tab=real-time` AND `?tab=batch`. Count with markers NOT present in the JS (e.g. `class="rt-seg"`, `id="rtUrl"`) — the JS selector strings contain `data-mode-toggle`/`rt-url-group`/`input_mode`, so naive substring checks false-positive.
