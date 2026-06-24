# MatchMiner

MatchMiner is a Tennis Intelligence Platform SaaS that delivers daily, AI-scored tennis insights mined and ranked from across the web.

> Note: the artifact directory/slug is still `permitlify` (the app started as "Permitlify" before the rebrand). The user-facing brand is **MatchMiner**. Renaming the slug/directory would break the workflow and proxy paths, so it is intentionally left as-is — only the title, wordmark, domain text, and logo are MatchMiner.

## Stack (Django rebuild)

The app was rebuilt from scratch in **Django** (Python), replacing the previous React+Vite frontend and the Express API. Django now serves every page and handles authentication directly.

- Python 3.11, Django 5.2
- DB: PostgreSQL (Django ORM). Users in `auth_user`, sessions in `django_session`. App data in `accounts_scraper` (the 9 tennis sources) and `accounts_run` (per-run history, logs, and three CSV outputs: items data, requests telemetry, errors telemetry).
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
  - `accounts/` — the app:
    - `models.py` — `Scraper` (slug/code/name/tour/domain/vendor_url/description/returns/`tournaments` JSON/`mode`/`maintenance_message`); `Run` (uuid, FK `scraper`, `launched_by` FK→user SET_NULL, tournament, date_from/to, status incl. `RUNNING`, started/finished, duration_ms, row_count, output_size_bytes, `log_text`, `csv_data` (items), `requests_csv`, `errors_csv`; helper props `is_maintenance`/`short_id`/`duration_label`/`size_label`/`has_csv`/`has_requests`/`has_errors`/`request_count`/`is_running`); and `RunLogLine` (FK `run`, `seq`, `level`, `text`) — one streamed log line per run, written incrementally by the background worker so the live console (and concurrent viewers) can poll. **Do not** add `run_count`/`last_run_at` as model props — they're query annotations. `Run` has a **partial unique constraint** `uniq_running_run_per_scraper` (`status='running'`) enforcing at most one in-flight run per scraper.
    - `runs.py` — now only holds the `ALL_TOURNAMENTS` constant used by views as the `Run.tournament` label. **All simulated/demo/seeded run generators were removed** — every run is a real live scrape.
    - `live_scrapers/` — real scrapers + their telemetry. `_stadion.py` is the shared, parameterised stdlib-`urllib` port of the production ITF/Stadion team-competition spider (direct, no proxy; `MAX_TIES=6` keeps a run bounded; 60-col Title-cased ITF CSV header; `sanitize_cell` injection guard). It exposes `StadionConfig` + `run(config, run_obj, log)` returning the 5-tuple `(items_csv, requests_csv, errors_csv, row_count, status)`. `billiejeankingcup.py` (Fed Cup, draw code `bjkc`, women) and `daviscup.py` (draw code `dc`, men) are thin wrappers: a `CONFIG` + `def run(run_obj, log): return _stadion.run(CONFIG, run_obj, log)` — same public API, different draw code / gender / match URL. `telemetry.py` — `Telemetry` records every HTTP request (`record_request`) and error (`record_error`) and emits `requests_csv()` / `errors_csv()` whose columns/formats match the user's production framework (requests: `request_id`=uuid4, `duration`="{ms:.0f} ms", `fingerprint`=sha1(url+status+size), `http_method`, `response_size`="{n} bytes", `http_status`, `last_seen`="%Y-%m-%d %H:%M:%S UTC", `url`; errors: `timestamp`="%Y-%m-%d %H:%M:%S.%f", `level`, `message`, `exception`=traceback). Add new real scrapers here and register them in `run_scrape`'s `LIVE_SCRAPERS`. Real years are derived from `Run.date_from`/`date_to` (the year dropdown sets Jan 1–Dec 31 of one year).
    - `management/commands/run_scrape.py` — the **background worker**. `python3 manage.py run_scrape <uuid>` runs in its own OS process (launched via `subprocess.Popen`, `start_new_session=True`). Its `_RunLogger` persists each line as a `RunLogLine` (and prints it). Dispatch is a `LIVE_SCRAPERS` registry (`billiejeankingcup`, `daviscup`); each returns the 5-tuple, which the worker persists to `csv_data`/`requests_csv`/`errors_csv` + status/row_count/size/duration. **No simulated fallback**: an unregistered slug fails the run honestly (status `FAILED`, a `Telemetry` errors CSV explaining no scraper is wired, no fabricated rows). On finish it materialises `Run.log_text`; a broad `except` writes the traceback into both the log and an errors CSV and marks the run `FAILED`.
    - `migrations/0002_seed_scrapers.py` — inline idempotent data migration seeding the 9 scrapers (incl. `billiejeankingcup` + `daviscup`).
    - `views.py` (`login_view`; `@login_required` `overview_view` / `scrapers_view` / `scraper_detail_view` / `scraper_run_view` / `run_events_view` / `run_log_view` / `run_log_download_view` / `run_csv_download_view` / `run_requests_download_view` / `run_errors_download_view` + placeholder pages; POST-only `logout_view`). `scraper_run_view` validates a single **year** field (`YEAR_MIN`=2000…`YEAR_MAX`=2030) and stores it as `date_from`=Jan 1 / `date_to`=Dec 31; the detail view passes `years` (descending) + `default_year` (current year, clamped) to the real-time tab. Helpers: `_launch_run` (Popen), `_reap_stale_runs` (fails runs stuck `RUNNING` past `STALE_RUNNING_AFTER`=20min), `_run_lines`/`_run_log_text` (live-rows-or-`log_text` fallback). `urls.py`.
  - `templates/` — `base.html` (html shell), `app_base.html` (authenticated layout: left sidebar + topbar with `breadcrumb`/`topbar_actions` blocks + theme toggle + `tr[data-href]` row-click that ignores anchors; defines a `content` block), `login.html`, `overview.html`, `scrapers.html`, `scraper_detail.html` (the Lab — tabbed; real-time tab branches on `active_run`: live console + polling JS vs. start form), `run_log.html` (paginated log viewer), `_placeholder.html`, `partials/` (`logo.html`, `scraper_table.html`, `pagination.html`). The sidebar marks the active item via `active_nav`.
  - `static/css/styles.css` — brand tokens + `--app-*` theme tokens (light `:root` + `html[data-theme="dark"]`) + login layout + app/lab component styles. Append new component CSS using the `--app-*` tokens.
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

Django's built-in auth. Seeded login: username `salman` (the password was set out-of-band, not stored in the repo). Flow verified end-to-end: login → `/overview/`, protected route redirects to `/` when unauthenticated, wrong password shows an inline error, logout returns to `/`.

## Product

- **Login** (`/`) — two-column page: dark sales/marketing panel left (headline with blue→green gradient, proof cards, trust badges), white sign-in form right (username/password). MatchMiner-branded (tennis-ball logo, wordmark, domain). Complete.
  - Caveat: the left-panel sales copy and proof cards are still permit-themed from the original concept (faithful port). Rewrite for tennis when updating body content.
- **Authenticated app** — pages behind login share `app_base.html` (left sidebar + topbar). DB-backed.
  - **Overview** (`/overview/`) — greets the user; three live stat cards (active scrapers, runs today, in maintenance) + a "Recently active" scraper table.
  - **Scrapers** (`/scrapers/`) — lists the 9 scrapers with Tour/Mode/Runs/Last-run (counts from query annotations) and an **Open lab** button → the detail page.
  - **Scraper Lab** (`/scrapers/<slug>/?tab=…`) — tabbed detail page (Real-time test, Calls history, Settings, Status), the core feature:
    - **Real-time test** — runs a **real scrape as a background OS process**. The start form is a single **year dropdown** (2000–2030, default = current year); there are no date inputs. It POSTs to `scraper_run`, which validates the year is in range, blocks when in maintenance, creates a `RUNNING` `Run` (with `date_from`=Jan 1 / `date_to`=Dec 31 of that year), then `subprocess.Popen`s the `run_scrape` worker and redirects back to the real-time tab. When a run is in flight the tab shows a **live console** that polls `run_events` (JSON, `?after=<seq>` cursor) ~1s, escapes+appends new lines, auto-scrolls, and on `done` reveals the summary + Open log / Download log / **CSV** (items) / **Requests CSV** / **Errors CSV** / "Run another" (each download disabled if that file is empty, driven by `has_csv`/`has_requests`/`has_errors` in the events JSON). The log streams live **and** persists. Concurrency: at most one in-flight run per scraper (DB partial-unique constraint + friendly pre-check); a stuck/orphaned `RUNNING` run is reaped (failed) by `_reap_stale_runs`, invoked from both the detail view and the events poller so a live console always terminates.
    - **Calls history** — paginated (12/page) list of runs: id, started, tournament, window, status pill, rows, size, duration, and per-run **Open log** (new tab), **Log** (.txt download), **CSV** (items), and **Requests** / **Errors** CSV downloads (shown when present). Routes are scoped by `uuid + scraper__slug` (no IDOR). Exactly one completed run lands here per real-time scrape.
    - **Status** — Production/Maintenance radio + maintenance message, persisted to `Scraper.mode`; gates real-time runs. Settings is a placeholder. (The former **Code samples** and **Enhancements** tabs were removed.)
  - **Run log viewer** (`/scrapers/<slug>/runs/<uuid>/log/`) — paginated (150 lines/page) log with metadata chips and Download log / CSV / Requests CSV / Errors CSV (the last three shown when present); opened via `target="_blank"`. Reads live `RunLogLine` rows while running, the materialised `log_text` snapshot once finished.

### Per-run CSV downloads (requests / errors / items)

Every run produces up to three CSVs, downloadable from the live console, Calls history, and the run-log viewer:
- **items** (`data.csv`, `Run.csv_data`) — the scraped rows; 60-col ITF schema with a **Title-cased** header (e.g. `Tie Id`, `Match Id`).
- **requests** (`requests.csv`, `Run.requests_csv`) — one row per HTTP call made during the run (see `telemetry.py` for exact columns/formats).
- **errors** (`errors.csv`, `Run.errors_csv`) — one row per failure (empty when a run had none).

### Real scrapers vs. the rest

Only the two ITF/Stadion team competitions are wired as real scrapers — **Billie Jean King Cup** and **Davis Cup** — because they share one public JSON API needing no proxies/credentials. The other ~7 seeded sources have no live implementation; starting a run for them **fails honestly** (no fabricated data). Wiring them would require the original framework's proxy pool / `curl_cffi` / Selenium / AI extraction, which isn't available in this environment.

## Legacy / cleanup

- `artifacts/api-server/` (Express) and `lib/api-spec/` are **no longer used** by MatchMiner — the Django app replaced them. They remain in the repo (and api-server's workflow still runs) but nothing depends on them. They can be removed on request.
- The old Express `users`/`session` Postgres tables are likewise unused; Django uses `auth_user`/`django_session`.

## User preferences

- Recreate the supplied designs faithfully/pixel-exact rather than improvising.

## Gotchas

- Run the app via the workflow, not `pnpm dev`. The workflow's cwd is the artifact dir.
- After model/settings changes, restart the `artifacts/permitlify: web` workflow. The server runs `--noreload`, so changes to Python (incl. `runs.py`, `run_scrape.py`, `live_scrapers/`) only take effect after a restart. **The `run_scrape` worker is a fresh `python3` process per run**, so it picks up worker-side code changes immediately — but the views that launch/serve it still need the restart.
- The worker shares the same `DATABASE_URL` as the web process (that's how live streaming works cross-process). Validate the whole flow with `django.test.Client` in `manage.py shell` (force_login + POST `{"year": …}` + poll `run_events`): it exercises the real subprocess + cross-process polling (real network calls to the ITF API) without the Secure-cookie/CSRF friction of `curl`.
- There is **no demo/seeded run data anymore** — runs are real live scrapes. Only `billiejeankingcup` and `daviscup` are wired; other slugs fail honestly. Don't reintroduce simulated runs.
- Keep brand tokens global in `static/css/styles.css` `:root`; scope new-page CSS under that page's root class. App/lab styles use the `--app-*` light/dark tokens.

## Pointers

- See the `pnpm-workspace` skill for monorepo structure (note: this artifact is now Python, not a pnpm package).
