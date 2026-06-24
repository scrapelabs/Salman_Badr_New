# MatchMiner

MatchMiner is a Tennis Intelligence Platform SaaS that delivers daily, AI-scored tennis insights mined and ranked from across the web.

> Note: the artifact directory/slug is still `permitlify` (the app started as "Permitlify" before the rebrand). The user-facing brand is **MatchMiner**. Renaming the slug/directory would break the workflow and proxy paths, so it is intentionally left as-is — only the title, wordmark, domain text, and logo are MatchMiner.

## Stack (Django rebuild)

The app was rebuilt from scratch in **Django** (Python), replacing the previous React+Vite frontend and the Express API. Django now serves every page and handles authentication directly.

- Python 3.11, Django 5.2
- DB: PostgreSQL (Django ORM). Users in `auth_user`, sessions in `django_session`.
- Server: gunicorn (production), `manage.py runserver` (development)
- Static: WhiteNoise (`CompressedManifestStaticFilesStorage`)
- Config helpers: `dj-database-url` (reads `DATABASE_URL`), `psycopg2-binary`
- Python deps are managed with `uv` into `.pythonlibs`; `python3` resolves there.

## Run & Operate

The app runs via the `artifacts/permitlify: web` workflow (do not run `pnpm dev`). All commands below run from inside `artifacts/permitlify/` (the workflow's working directory **is** the artifact dir):

- Dev server: `python3 manage.py runserver 0.0.0.0:$PORT --noreload` (wired in `artifact.toml`)
- `python3 manage.py migrate` — apply migrations (creates `auth_user`, `django_session`, etc.)
- `python3 manage.py createsuperuser` / `manage.py shell` — manage users
- `python3 manage.py collectstatic --noinput` — gather static files (prod build step)
- Required env: `DATABASE_URL` (Postgres). Optional: `DJANGO_SECRET_KEY` (falls back to `SESSION_SECRET`), `DJANGO_DEBUG` (default `True`; set `False` in prod).

## Where things live

- `artifacts/permitlify/` — the Django project, previewPath `/`.
  - `manage.py` — Django entrypoint.
  - `matchminer/settings.py` — settings (DB, cookies, proxy, static). See "Replit integration" below.
  - `matchminer/urls.py` — root URL conf (includes `accounts`).
  - `accounts/` — auth app: `views.py` (`login_view`, plus `@login_required` `dashboard_view` / `scraper_directory_view` / `run_history_view`, and POST-only `logout_view`), `urls.py`.
  - `templates/` — `base.html` (html shell), `app_base.html` (authenticated layout with the left sidebar; defines a `content` block), `login.html`, `dashboard.html`, `scraper_directory.html`, `run_history.html`, `partials/logo.html` (tennis-ball SVG logo, takes a `uid` for unique gradient IDs). The sidebar marks the active item via `request.resolver_match.url_name`.
  - `static/css/styles.css` — brand tokens (`:root`) + login layout (ported from the old `login.css`/`index.css`) + dashboard styles.
  - `static/favicon.svg` — preserved from the original app.
  - `.replit-artifact/artifact.toml` — repurposes the `web` artifact to run Django (see below).
- `attached_assets/dailypermit_*.html` — original supplied design mockups (source of truth for each page's look).

## Replit integration (important, non-obvious)

The Replit artifacts framework has no Python/Django kind, so the existing `web` artifact slot is repurposed by hand-editing `artifact.toml` (validated via `verifyAndReplaceArtifactToml`). Key facts learned:

- The workflow runs the `run` command with **cwd = the artifact directory** (`artifacts/permitlify`), not the workspace root. Use plain relative commands (`python3 manage.py ...`) — no `cd`, no path prefix.
- `verifyAndReplaceArtifactToml` **cannot change `integratedSkills`**, so the original `react-vite` `integratedSkills` block is kept byte-for-byte even though it's now Django (harmless metadata).
- `--noreload` is used so Django's file watcher doesn't choke on the monorepo.
- The preview is a **cross-site iframe**, so `settings.py`: omits `XFrameOptionsMiddleware`, sets session/CSRF cookies `SameSite=None; Secure`, sets `SECURE_PROXY_SSL_HEADER`, and trusts Replit domains in `CSRF_TRUSTED_ORIGINS`.
- Because cookies are `Secure`, test auth over the **HTTPS** dev domain (`$REPLIT_DEV_DOMAIN`); `curl` will not send Secure cookies over plain `http://localhost`.

## Auth & seed account

Django's built-in auth. Seeded login: username `salman` (the password was set out-of-band, not stored in the repo). Flow verified end-to-end: login → `/dashboard/`, protected route redirects to `/` when unauthenticated, wrong password shows an inline error, logout returns to `/`.

## Product

- **Login** (`/`) — two-column page: dark sales/marketing panel left (headline with blue→green gradient, proof cards, trust badges), white sign-in form right (username/password). MatchMiner-branded (tennis-ball logo, wordmark, domain). Complete.
  - Caveat: the left-panel sales copy and proof cards are still permit-themed from the original concept (faithful port). Rewrite for tennis when updating body content.
- **Authenticated app** — pages behind login share `app_base.html`, which renders a left **sidebar**: brand, nav (Dashboard → Scraper Directory → Run History), and a footer with the user + logout.
  - **Dashboard** (`/dashboard/`) — greets the user; three stat cards (placeholder values).
  - **Scraper Directory** (`/scraper-directory/`) — placeholder; will list scraper endpoints (see `attached_assets/Image20260624020035_*.png` reference).
  - **Run History** (`/run-history/`) — placeholder; will show per-run status/source/size/duration.

## Legacy / cleanup

- `artifacts/api-server/` (Express) and `lib/api-spec/` are **no longer used** by MatchMiner — the Django app replaced them. They remain in the repo (and api-server's workflow still runs) but nothing depends on them. They can be removed on request.
- The old Express `users`/`session` Postgres tables are likewise unused; Django uses `auth_user`/`django_session`.

## User preferences

- Recreate the supplied designs faithfully/pixel-exact rather than improvising.

## Gotchas

- Run the app via the workflow, not `pnpm dev`. The workflow's cwd is the artifact dir.
- After model/settings changes, restart the `artifacts/permitlify: web` workflow.
- Keep brand tokens global in `static/css/styles.css` `:root`; scope new-page CSS under that page's root class.

## Pointers

- See the `pnpm-workspace` skill for monorepo structure (note: this artifact is now Python, not a pnpm package).
