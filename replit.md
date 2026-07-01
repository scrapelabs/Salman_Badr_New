# MatchMiner

MatchMiner is a Tennis Intelligence Platform SaaS delivering daily, AI-scored tennis insights mined and ranked from across the web.

> **Naming:** the artifact dir/slug is still `permitlify` (the app began as "Permitlify" before the rebrand). Don't rename it — that breaks the workflow and proxy paths. Only the title, wordmark, domain text, and logo are MatchMiner.

## Stack

Django (Python), serving every page and handling auth directly. Rebuilt from scratch, replacing an old React+Vite + Express stack that is now fully removed (see "Legacy").

- Python 3.11, Django 5.2. Server: gunicorn (prod Linux) / `manage.py runserver` (Replit dev workflow) / **Waitress** (local Windows, `3_run_server.bat`).
- PostgreSQL via Django ORM (`dj-database-url` reads `DATABASE_URL`; `psycopg2-binary`). Django tables `auth_user` / `django_session`; app data in `accounts_*`.
- Static via WhiteNoise (`CompressedManifestStaticFilesStorage`).
- Scraper HTTP: `curl_cffi` (Chrome TLS impersonation). Some upstreams (ITF/Stadion behind CloudFront) 403-block datacenter IPs → they need impersonation **plus** a residential proxy.
- Python deps via `uv` into `.pythonlibs` (`pyproject.toml`/`uv.lock`); root `requirements.txt` mirrors them for the Windows pip flow.

## Run & Operate

Runs via the **`artifacts/permitlify: web`** workflow (not `pnpm dev`). The workflow cwd **is** the artifact dir, so use plain relative commands:

- Dev server: `python3 manage.py runserver 0.0.0.0:$PORT --noreload` (wired in `artifact.toml`).
- `migrate` / `createsuperuser` / `shell` / `collectstatic --noinput` as usual.
- Env: **`DATABASE_URL`** required. Optional: `DJANGO_SECRET_KEY` (falls back to `SESSION_SECRET`), `DJANGO_DEBUG` (default `True`), `DJANGO_LOCAL_HTTP` (default `False`), `MATCHMINER_SCHEDULER_ENABLED` (kill-switch).
- **After Python/settings changes, restart the workflow** (it runs `--noreload`). The `run_scrape` worker is a fresh process per run so it picks up worker-side changes immediately; the views that launch/serve it do not.

### Local Windows dev (`bat_files/`)

Double-click helpers at the workspace root:

- `0_setup.bat` — one-stop install/update: `git pull`, create `.venv` + `pip install -r requirements.txt`, copy `.env.example`→`.env` on first run (halts so you can fill `DATABASE_URL`), else `migrate` + `collectstatic`.
- `1_migrate.bat` — quick post-pull update: `migrate` + `collectstatic` only (no pull/reinstall). Run after pulling a new migration and/or static change.
- `3_run_server.bat` — serves via **Waitress on port 80** (`matchminer.wsgi`); run **as Administrator** (privileged port, must be free). Static is WhiteNoise, so run a collectstatic helper first or assets 404.
- `4_create_superadmin.bat` — interactive `createsuperuser`. `6`–`9_scrape_*.bat` — per-scraper **synchronous** `scrape_now` runs (same validate + dispatch path as the web "Real-time test" button), dumping CSVs/log under git-ignored `scrape_output/<slug>/<run-id>/`.
- **All bats call the venv interpreter by full path** (`%~dp0..\.venv\Scripts\python.exe`), never `activate` + bare `python` — on Windows bare `python` can resolve to a system Python that lacks deps (`No module named 'dj_database_url'`).
- Config from a root `.env` (git-ignored; template `.env.example`), loaded **non-overriding** so real Replit env vars always win.

**Local-HTTP cookie gotcha:** session/CSRF cookies are `Secure`+`SameSite=None` for the Replit iframe, and Secure cookies aren't sent over plain `http://localhost` — so login fails locally. Set `DJANGO_LOCAL_HTTP=True` (the `.env.example` default) to switch them to non-Secure `Lax`. The var is unset on Replit, so the hosted preview keeps secure cross-site cookies.

## Where things live

`artifacts/permitlify/` — the Django project (previewPath `/`).

- `matchminer/` — `settings.py` (DB, cookies, proxy, static; see "Replit integration"), `urls.py`.
- `accounts/` — the app:
  - `models.py` — `Proxy`, `Scraper`, `Run`, `RunLogLine`, `ScraperSchedule`, `ScheduleEvent`, `CollegeMatch`, `SAKey`, `ScraperModelFile`, plus QA models (ticket/comment/notification/attachment).
  - `live_scrapers/` — real scrapers, the `LIVE_SCRAPERS` registry, shared HTTP clients (`_http`, `_browser`), and `telemetry.py` (requests/errors CSVs in the production framework's exact columns).
  - `management/commands/` — `run_scrape` (the background worker: `subprocess.Popen`, own process-group; an unregistered slug fails honestly) and `scrape_now` (synchronous CLI equivalent).
  - `scheduler.py` / `scheduling.py` (in-app cron), `qa_views.py`, `sanitize.py`, `context_processors.py`, `migrations/`.
- `templates/` — `base.html`, `app_base.html` (sidebar/topbar, theme toggle, `tr[data-href]` row-click, live server-time clock), per-page templates, `partials/`. `scraper_detail.html` is the tabbed Lab.
- `static/css/styles.css` — brand tokens (`:root`) + `--app-*` light/dark theme tokens + components. `static/favicon.svg` preserved from the original.
- `.replit-artifact/artifact.toml` — repurposes the `web` artifact to run Django.
- `attached_assets/dailypermit_*.html` — original supplied design mockups (visual source of truth per page).

Field-level and view-level details are in the code; the non-obvious mechanics live in `.agents/memory/` (referenced throughout below).

## Replit integration (non-obvious)

The artifacts framework has no Python/Django kind, so the `web` slot is repurposed by hand-editing `artifact.toml` (validated via `verifyAndReplaceArtifactToml`):

- Workflow cwd = the artifact dir; relative `python3 manage.py …` (no `cd`, no path prefix).
- `verifyAndReplaceArtifactToml` can't change `integratedSkills`, so the original `react-vite` block is kept byte-for-byte (harmless metadata).
- `--noreload` keeps Django's file watcher from choking on the monorepo.
- The preview is a **cross-site iframe**: `settings.py` omits `XFrameOptionsMiddleware`, sets cookies `SameSite=None; Secure`, sets `SECURE_PROXY_SSL_HEADER`, and trusts Replit domains in `CSRF_TRUSTED_ORIGINS`. Because cookies are Secure, test auth over the **HTTPS** dev domain (`$REPLIT_DEV_DOMAIN`). See the `django-on-replit-artifact` memory.

## Auth

Django's built-in auth. Seeded login: username `salman` (password set out-of-band, not in the repo). Add admins with `createsuperuser` (or `4_create_superadmin.bat`).

## Product

- **Login** (`/`) — two-column page: dark marketing panel + white sign-in form, MatchMiner-branded. *Caveat:* the left-panel sales copy is still permit-themed from the original port — rewrite for tennis when touching it.
- **Authenticated app** (shares `app_base.html`, DB-backed):
  - **Overview** (`/overview/`) — greeting + three live stat cards + a recently-active table.
  - **Scrapers** (`/scrapers/`) — list with Tour/Mode/Runs/Last-run + "Open lab".
  - **Scraper Lab** (`/scrapers/<slug>/?tab=…`) — the core feature, tabbed:
    - **Real-time test** — runs a real scrape as a background process; a live console polls `run_events` (~1s) with a confirm-guarded **Stop run**, and reveals the summary + log/CSV downloads on completion. Max one in-flight run per scraper (DB constraint + reaping).
    - **Calls history** — paginated runs with per-run log + items/requests/errors CSV downloads (scoped by uuid+slug, no IDOR).
    - **Schedule** — in-app recurring runs (daily / weekly / biweekly / monthly + time-of-day + IANA timezone), fired by a daemon thread in the web process via the same start path as the button/webhook (at-most-once, no backfill; multi-worker-safe via a Postgres advisory lock). A **Cron history** panel logs each fire's outcome. A `@csrf_exempt` Bearer-token webhook (`POST …/trigger/`) remains as a programmatic backend (token is sensitive — never log it; no longer surfaced in the UI). See the `in-app-scheduler` memory.
    - **Settings** (admin-only) — proxy (or direct) + worker-threads (1–16) + tries-per-request (1–10); plus a captcha-model upload panel for scrapers that declare one. See the `retry-budget-setting` memory.
    - **Status** — Production/Maintenance radio + message; gates runs.
    - **Match database** (`?tab=data`, `college_dual_match` only) — stored `CollegeMatch` rows + headline stats + "Download all" / "Download by date" CSV export (filtered by match date). See the `college-match-store` memory.
  - **Settings** (`/settings/`, superuser-only, in the Workspace nav group) — merged workspace-config page with three sections: **General** (workspace-wide Anthropic API key — `GeneralConfig` singleton, masked on display, never logged), **Proxy pools** (add/delete pools; addresses always rendered via `display_address` (masked); per-scraper selection lives in the Lab's Settings tab), and **Passwords** (per-user change-password modal, Django validation). The old standalone `/proxies/` route now 302-redirects here. The Anthropic key resolves per-scraper `Scraper.claude_api_key` → `GeneralConfig` → env `settings.CLAUDE_KEYS`.
  - **QA Team Tasks** (`/qa/`) — Jira-like ticketing per scraper: Kanban board, create/edit modals with Quill rich text + inline image upload, ticket detail with a comments thread, superuser-only delete. Body HTML is server-sanitized (`accounts/sanitize.py`, nh3 allowlist); inline images upload to `/qa/attachments/` (magic-byte sniff, 5MB, no SVG). See the `qa-rich-text-sanitization` memory.
  - **Notifications** — a navbar bell polling `/qa/notifications/poll/`, fanned out by `_notify()` on ticket-created / comment-added / status-changed.
  - **Users** (`/users/`) — superuser-only CRUD with Django password validation + self/last-superuser protections.
  - **Run log viewer** — paginated log + downloads (live `RunLogLine` while running, the `log_text` snapshot once finished).

**Per-run CSVs** — up to three per run: **items** (`data.csv`), **requests** (`requests.csv`), **errors** (`errors.csv`, empty when none). See `telemetry.py` for exact columns.

**Scraper catalogue** — **39 wired scrapers** (39 `LIVE_SCRAPERS` entries == 39 seeded `Scraper` rows), the full supplied source set ported deterministically. Shared engines: **Stadion/ITF** (`_stadion.py`: BJK/Davis Cup), **TS tournaments** (`_ts_tournament.py`), **TS leagues** (`_ts_league.py`), **itftennis** (`_itftennis.py`), **rankings** (`_rankings.py`); plus standalone own-parser sources (czech, uruguay, maxpreps, ioncourt, prestosports, new_jersey_high_school, estonia, belgium_results, the queue-driven south_africa, and australia_tennis / poland_results / usta_team_captains / college_dual_match). Several **honest-fail until their creds/infra are present** (residential proxy, env creds, or region) — they emit a correct empty 5-tuple + errors CSV, never fabricated rows. Unwired slugs also fail honestly (assigning a proxy doesn't make one work — only a registry entry + runner does). **SSRF is enforced centrally** in `_http.ScraperClient.request()`. See the `matchminer-live-scrapers` memory.

## Legacy / cleanup

The old React+Vite frontend + Express API (and the `lib/api-spec`, `lib/api-client-react`, `lib/api-zod`, `lib/db` Drizzle packages) are **removed** — MatchMiner is Django-only (`artifacts/permitlify`). The canvas design tool (`artifacts/mockup-sandbox`) is unrelated infrastructure and stays. The old Express `users`/`session` Postgres tables are unused (Django uses `auth_user`/`django_session`) and can be dropped on request.

## User preferences

- Recreate supplied designs faithfully/pixel-exact rather than improvising.
- Keep code synced to the GitHub repo (`scrapelabs/Salman_Badr_New`, branch `main`) automatically after finishing work — do **not** ask each time. **Push committed checkpoints directly to `main`** (fast-forward/normal push only, never force-push); if histories diverge, stop and ask. Note the one-turn commit lag: a turn's edits become a commit only at end-of-turn, so they reach `main` on the *next* turn.

## Gotchas

- Run via the workflow, not `pnpm dev`; restart it after Python/settings changes (`--noreload`).
- The worker shares the web process's `DATABASE_URL` (how live cross-process streaming works). Validate the full flow with `django.test.Client` in `manage.py shell` (force_login + POST + poll `run_events`) — avoids the Secure-cookie/CSRF friction of `curl`.
- All runs are **real live scrapes** — no demo/seeded runs; don't reintroduce simulated ones.
- `south_africa` is the lone **queue-driven** scraper (`has_key_store`): it works a queue of `SAKey` tournament keys (seeded from `tournament_keys.txt`) rather than a date/year. Run/monitor it from the **Real-time** tab (paste-keys textarea + run-all checkbox + live console). The old dedicated **Key queue** tab was removed (user request): `?tab=keys` now redirects to `?tab=real-time`. The SportyHQ `X-API-KEY` is the site's **public** results key (not a secret). See the `south-africa-scraper` memory.
- `belgium_results` sits behind a **Zenedge captcha**: a live run needs TensorFlow (wired — `tensorflow-cpu` Linux / `tensorflow` Windows, pinned **2.18 / Keras 3**) **plus** a `captcha_model.keras` uploaded via the Lab's Settings tab (stored in Postgres, materialized to disk by the worker). Without it the solver honest-fails like Stadion without a proxy; Zenedge may also block datacenter IPs. See the `belgium-captcha-honest-fail` memory.
- A `Proxy.address` may embed credentials — **never render the raw `address`**, always use `display_address`; scrapers must never log it.
- Keep brand tokens global in `styles.css` `:root`; scope new-page CSS under that page's root class; use the `--app-*` tokens.

## Pointers

- `.agents/memory/` holds the non-obvious mechanics (scheduler, live scrapers, captcha, dedup, retry budget, Django-on-Replit, GitHub auto-push, etc.) — check it before touching those areas.
- See the `pnpm-workspace` skill for monorepo structure (note: this artifact is Python, not a pnpm package).
