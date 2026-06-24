---
name: Global confirm dialog convention
description: MatchMiner replaced native confirm() with a reusable themed modal; how new destructive actions must opt in.
---

MatchMiner has a single global confirm dialog (`#mmConfirm`) defined once in `templates/app_base.html` (markup + a small IIFE), styled as `.mm-confirm*` in `styles.css`.

**Rule:** never use native `confirm()` / `onsubmit="return confirm(...)"` for destructive form submits. Instead put these attributes on the `<form>`:
`data-confirm="<message>"`, optional `data-confirm-title`, `data-confirm-ok` (button label), `data-confirm-tone` (`danger` default, or `primary`). The global JS intercepts `submit`, shows the modal, and on OK re-submits the same form once.

**Why:** the user asked for a "beautiful modal, not the native JS popup". Native confirm() is unstyled and inconsistent with the design system.

**How to apply:**
- Re-entrancy guard: the JS sets `form.__mmConfirmed` then calls `requestSubmit()`; the second submit event sees the flag, clears it, and proceeds — so exactly one native submission, no loop.
- Works for forms submitted via an external `button[type=submit][form=…]` (e.g. the calls-tab bulk-delete) because clicking still fires the form's `submit` event.
- The dialog only needs to exist on pages extending `app_base.html` (all authenticated app pages do).
