---
name: Django shell validation seeds persist (autocommit)
description: Seeding test rows in `manage.py shell` to validate a view leaks them into the real DB — savepoint rollback is a no-op there.
---

The project's validation pattern is "exercise the flow with `django.test.Client` in `manage.py shell`" (force_login + GET/POST). If that validation **creates rows** (e.g. seeding N tickets to force pagination), those rows are **committed to the real database** — `manage.py shell` runs in autocommit mode, so `transaction.savepoint()` / `savepoint_rollback()` silently do nothing and the objects survive the shell exiting.

**Why:** it happened — seeding 23 tickets to test the QA pager left all 23 in the live DB; the savepoint_rollback reported success but deleted nothing.

**How to apply:** when a shell validation must create data, either
- wrap it in `with transaction.atomic():` and raise at the end to roll back, or
- delete exactly what you created afterwards (capture the pks / filter on the unique test titles) and re-query the count to confirm it's back to the pre-test number.

Prefer read-only assertions (status codes, substring checks on rendered HTML, the AJAX JSON shape) over seeding when you can. `django.test.TestCase` wraps each test in a transaction, but a bare `manage.py shell` does not.
