---
name: DB schema management (MatchMiner)
description: Django migrations are the ONLY schema path; never run the legacy Drizzle "db push" against the shared DATABASE_URL.
---

# MatchMiner DB schema management

The single Postgres `DATABASE_URL` is owned by the Django app (`artifacts/permitlify`). Schema changes happen **only** via `python3 manage.py migrate`.

**Why:** The repo still contains the legacy Express/Drizzle `db` package. Its `pnpm --filter db push` (drizzle-kit) does not know about Django's tables (`auth_user`, `django_session`, `accounts_*`) and will offer to **DROP them all**. It once sat in `scripts/post-merge.sh` and ran on every task merge; it only avoided data loss because the interactive confirm prompt got EOF (no TTY) and aborted. If it ever ran with `--force` / a TTY it would wipe all app + auth data.

**How to apply:**
- Keep `scripts/post-merge.sh` on the Django path: `pnpm install --frozen-lockfile` then `cd artifacts/permitlify && python3 manage.py migrate --noinput`. Never reintroduce `pnpm --filter db push` there.
- Never point any Drizzle `db push`/`migrate` at the project `DATABASE_URL`.
- Removing/archiving the legacy `lib/api-spec` + `artifacts/api-server` + `db` package would permanently remove this footgun.
