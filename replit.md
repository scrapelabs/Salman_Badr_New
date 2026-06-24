# MatchMiner

MatchMiner is a Tennis Intelligence Platform SaaS delivering daily, AI-scored tennis insights mined and ranked from across the web.

> **Naming:** the artifact dir/slug is still `permitlify` (the app began as "Permitlify" before the rebrand). Don't rename it — that would break the workflow and proxy paths. Only the title, wordmark, domain text, and logo are MatchMiner.

## Stack

Rebuilt from scratch in **Django** (Python), replacing the old React+Vite frontend and Express API. Django serves every page and handles auth directly.

- Python 3.11, Django 5.2; gunicorn (prod) / `manage.py runserver` (dev).
- PostgreSQL via Django ORM (`dj-database-url` reads `DATABASE_URL`; `psycopg2-binary`). Auth in `auth_user`, sessions in `django_session`; app data in `accounts_scraper` / `accounts_run` / `accounts_proxy`.
- Static via WhiteNoise (`CompressedManifestStaticFilesStorage`).
- Scraper HTTP client: `curl_cffi` (Chrome TLS impersonation). The upstream ITF/Stadion API sits behind CloudFront, which 403-blocks datacenter/cloud IPs — so requests need impersonation **plus** a residential proxy.
- Python deps managed with `uv` into `.pythonlibs`.

## Run & Operate

Runs via the `artifacts/permitlify: web` workflow (not `pnpm dev`). The workflow's cwd **is** the artifact dir (`artifacts/permitlify/`), so use plain relative commands:

- Dev server: `python3 manage.py runserver 0.0.0.0:$PORT --noreload` (wired in `artifact.toml`).
- `python3 manage.py migrate` — apply migrations.
- `python3 manage.py createsuperuser` / `shell` — manage users.
- `python3 manage.py collectstatic --noinput` — gather static (prod build step).
- Env: **`DATABASE_URL`** required. Optional: `DJANGO_SECRET_KEY` (falls back to `SESSION_SECRET`), `DJANGO_DEBUG` (default `True`; `False` in prod), `DJANGO_LOCAL_HTTP` (default `False`; `True` for local HTTP — see below).

After Python/settings changes, **restart the workflow** (it runs `--noreload`). The `run_scrape` worker is a fresh process per run, so it picks up worker-side changes immediately — but the views that launch/serve it still need the restart.

### Local Windows dev (`bat_files/` helpers)

`bat_files/` (at the workspace root) holds numbered double-click helpers, run in order: `0_install.bat` (venv + `pip install -r requirements.txt` + copies `.env.example`→`.env`), `1_migrate.bat`, `2_collectstatic.bat`, `3_run_server.bat` (`runserver 0.0.0.0:8000`), plus `4_create_superadmin.bat` (interactive `createsuperuser`) and `5_update_from_github.bat` (`git pull origin main`). Each uses `%~dp0..` to reach the workspace root (root `.venv`, `requirements.txt`, `.env`/`.env.example`) and `cd`s into `artifacts\permitlify`. `requirements.txt` (root) mirrors `pyproject.toml` for the pip flow. Config comes from a root `.env` (git-ignored; template `.env.example`), loaded by `settings.py` via **non-overriding** `python-dotenv` so real Replit env vars always win (a no-op when no `.env` exists).

**Local-HTTP cookie gotcha:** session/CSRF cookies are `Secure`+`SameSite=None` for the Replit iframe, and Secure cookies are never sent over plain `http://localhost:8000`, so login fails locally — set `DJANGO_LOCAL_HTTP=True` (the `.env.example` default) to switch them to non-Secure `Lax`. The var is unset on Replit, so the hosted preview keeps the secure cross-site cookies.

## Where things live

`artifacts/permitlify/` — the Django project (previewPath `/`).

- `matchminer/` — `settings.py` (DB, cookies, proxy, static; see "Replit integration"), `urls.py` (root conf, includes `accounts`).
- `accounts/` — the app:
  - `models.py` — `Proxy` (name/`kind`/optional `address` that may carry credentials/`is_active`; `display_address` masks the password), `Scraper` (slug/code/name/tour/`mode` + `proxy` FK, `threads` 1–16 default 5, `trigger_token` for the schedule webhook — sensitive, never log it), `Run` (uuid, status incl. `RUNNING`/`STOPPED`, `pid`, timing/size fields + three CSV blobs; partial-unique constraint `uniq_running_run_per_scraper` = at most one in-flight run per scraper), `RunLogLine` (one streamed log line per run for the live console).
  - `live_scrapers/` — real scrapers + telemetry. `_stadion.py` is the parameterised `curl_cffi` port of the production ITF/Stadion team-competition spider (scrapes all ties concurrently via a `ThreadPoolExecutor` sized by `Scraper.threads`; returns the 5-tuple `(items_csv, requests_csv, errors_csv, row_count, status)`); honours the scraper's proxy (returns a proxies dict only when the proxy is active with a non-empty address, else direct) and **never logs the address**. `billiejeankingcup.py` is a thin `StadionConfig` wrapper — sibling competitions (e.g. Davis Cup) can be added the same way. `telemetry.py` emits the requests/errors CSVs in the production framework's exact column format.
  - `management/commands/run_scrape.py` — the **background worker** (`subprocess.Popen`, own session/process-group). Dispatches via the `LIVE_SCRAPERS` registry; persists log lines + CSVs + status/row-count/size/duration. An unregistered slug **fails honestly** (`FAILED`, errors CSV, no fabricated rows).
  - `migrations/` — includes the idempotent data migration seeding the lone `billiejeankingcup` scraper.
  - `views.py` / `urls.py` — login/logout, overview, scrapers list, the Lab detail view (tabs), run lifecycle (start/stop/events/log + CSV downloads), the `@csrf_exempt` schedule webhook, proxies CRUD. The detail-view POST branches on a hidden `form` field (`settings` / `schedule-rotate-token` / status). Real-time start + webhook share `_start_scraper_run`; the worker is launched/killed via process-group helpers; stale `RUNNING` runs are reaped after ~20 min.
- `templates/` — `base.html`, `app_base.html` (sidebar + topbar, theme toggle, `tr[data-href]` row-click), per-page templates, and `partials/`. `scraper_detail.html` is the tabbed Lab.
- `static/css/styles.css` — brand tokens (`:root`) + `--app-*` light/dark theme tokens + component styles.
- `static/favicon.svg` — preserved from the original app.
- `.replit-artifact/artifact.toml` — repurposes the `web` artifact to run Django.
- `attached_assets/dailypermit_*.html` — original supplied design mockups (visual source of truth per page).

## Replit integration (non-obvious)

The artifacts framework has no Python/Django kind, so the `web` slot is repurposed by hand-editing `artifact.toml` (validated via `verifyAndReplaceArtifactToml`):

- Workflow cwd = the artifact dir; use relative `python3 manage.py ...` commands (no `cd`, no path prefix).
- `verifyAndReplaceArtifactToml` **cannot change `integratedSkills`**, so the original `react-vite` block is kept byte-for-byte (harmless metadata).
- `--noreload` keeps Django's file watcher from choking on the monorepo.
- The preview is a **cross-site iframe**: `settings.py` omits `XFrameOptionsMiddleware`, sets cookies `SameSite=None; Secure`, sets `SECURE_PROXY_SSL_HEADER`, and trusts Replit domains in `CSRF_TRUSTED_ORIGINS`. Because cookies are Secure, test auth over the **HTTPS** dev domain (`$REPLIT_DEV_DOMAIN`) — `curl` won't send Secure cookies over `http://localhost`.

## Auth

Django's built-in auth. Seeded login: username `salman` (password set out-of-band, not stored in the repo). Add admins with `createsuperuser` (or `bat_files/4_create_superadmin.bat` locally).

## Product

- **Login** (`/`) — two-column page: dark marketing panel (gradient headline, proof cards, trust badges) + white sign-in form. MatchMiner-branded. *Caveat:* the left-panel sales copy is still permit-themed from the original port — rewrite for tennis when touching body content.
- **Authenticated app** (shares `app_base.html`, DB-backed):
  - **Overview** (`/overview/`) — greeting + three live stat cards (active scrapers, runs today, in maintenance) + a "recently active" table.
  - **Scrapers** (`/scrapers/`) — list with Tour/Mode/Runs/Last-run + "Open lab".
  - **Scraper Lab** (`/scrapers/<slug>/?tab=…`) — the core feature, tabbed:
    - **Real-time test** — runs a real scrape as a background process. Start form = a single **year dropdown** (2000–2030, default current year). While in flight, a **live console** polls `run_events` (~1s, `?after=<seq>` cursor) and shows a runbar with a confirm-guarded **Stop run** button (force-kills the worker → `STOPPED`); on completion it reveals the summary + log/CSV downloads. Max one in-flight run per scraper (DB constraint + pre-check); stuck runs are reaped.
    - **Calls history** — paginated run list with per-run Open log / Log (.txt) / items + requests + errors CSV downloads. Routes scoped by uuid + slug (no IDOR).
    - **Schedule** — docs-only guide to schedule via **GitHub Actions**: masked trigger URL + token (reveal/copy), numbered setup steps, copy-ready workflow YAML, curl example, and a Regenerate-token button. The webhook `POST /scrapers/<slug>/trigger/` (`@csrf_exempt`) auths via `Authorization: Bearer <token>` (constant-time compare, never logged) and launches the same `_start_scraper_run` helper. Statuses: 401 / 400 / 409 / 503 / 201.
    - **Settings** — "Routing & performance": pick the `Proxy` (or Direct connection) + worker-threads (1–16) → saves `Scraper.proxy` + `Scraper.threads`.
    - **Status** — Production/Maintenance radio + message; gates real-time runs.
  - **Proxies** (`/proxies/`) — manage proxy pools (name + type + optional address), counts by type, "Used by", delete. Addresses rendered via `display_address` (masked). Per-scraper selection lives in the Lab's Settings tab.
  - **QA Team Tasks** (`/qa/`) — Jira-like ticketing per scraper. Kanban board (To Do / In Progress / Done) with a scraper filter; create modal (scraper dropdown, title, priority, Quill rich-text body with inline image upload). Ticket detail (`/qa/t/<uuid>/`) renders the **server-sanitized** body, a comments thread (also Quill), and a sidebar to set status/priority/assignee. Rich text is locked down in `accounts/sanitize.py` (nh3 allowlist) and rendered with `|safe`; see the `qa-rich-text-sanitization` memory for the lockstep rules. Inline images upload to `/qa/attachments/` (login-gated, magic-byte sniff PNG/JPEG/GIF/WebP, 5MB, no SVG, `nosniff`) and are served back from the same route; the editor uploads on toolbar-pick/paste/drop and inserts the URL, never base64. Backend lives in `accounts/qa_views.py`; Quill 2.0.3 is vendored under `static/vendor/quill/`.
  - **Notifications** — a navbar bell (`partials/notifications_bell.html`, polls `/qa/notifications/poll/` ~30s) fed by `accounts/context_processors.py`. `_notify()` fans out a `Notification` to all active users except the actor on ticket-created / comment-added / status-changed. Bell supports mark-all-read (`/qa/notifications/read-all/`) and click-through (`/qa/notifications/<id>/open/` marks read + redirects to the ticket).
  - **Users** (`/users/`) — **superuser-only** CRUD (add / edit / activate / deactivate / delete) with Django password validation. Protections: can't delete/demote/deactivate yourself, and can't remove the last active superuser. Non-superusers are redirected away. Mirrors the `proxies_view` POST-action pattern; edit uses a small modal pre-filled from row `data-*` attributes.
  - **Run log viewer** (`/scrapers/<slug>/runs/<uuid>/log/`) — paginated log + downloads; reads live `RunLogLine` rows while running, the materialised `log_text` snapshot once finished.

**Per-run CSVs** — each run produces up to three downloadable CSVs: **items** (`data.csv`, 60-col Title-cased ITF schema), **requests** (`requests.csv`, one row per HTTP call), **errors** (`errors.csv`, one row per failure; empty when none). See `telemetry.py` for exact columns.

**Scraper catalogue** — deliberately trimmed to a **single** wired scraper, **Billie Jean King Cup**, to perfect before adding more. It needs a **residential proxy assigned** (Lab → Settings) to return data (CloudFront blocks datacenter IPs). Any slug with no `LIVE_SCRAPERS` entry **fails honestly** — there is no simulated/demo data anywhere. Assigning a proxy doesn't make an unwired scraper work; only a registry entry does. The `_stadion.py` engine is parameterised, so sibling ITF/Stadion competitions can be re-added as thin wrappers; other source types would need Selenium / AI extraction, which isn't available here.

## Legacy / cleanup

`artifacts/api-server/` (Express) and `lib/api-spec/` are unused by MatchMiner (Django replaced them) but remain in the repo; api-server's workflow still runs. The old Express `users`/`session` Postgres tables are unused too (Django uses `auth_user`/`django_session`). Removable on request.

## User preferences

- Recreate supplied designs faithfully/pixel-exact rather than improvising.
- Keep code synced to the GitHub repo (`scrapelabs/Salman_Badr_New`, branch `main`) automatically after finishing work — do **not** ask each time. **Push committed checkpoints directly to `main`** (fast-forward/normal push only, never force-push); if histories diverge, stop and ask. Note the one-turn commit lag: a turn's edits become a commit only at end-of-turn, so they reach `main` on the *next* turn.

## Gotchas

- Run via the workflow, not `pnpm dev`. After Python/settings changes, restart the `artifacts/permitlify: web` workflow (`--noreload`).
- The worker shares the same `DATABASE_URL` as the web process (how live cross-process streaming works). Validate the full flow with `django.test.Client` in `manage.py shell` (force_login + POST `{"year": …}` + poll `run_events`) — exercises the real subprocess + cross-process polling without the Secure-cookie/CSRF friction of `curl`.
- No demo/seeded runs — all runs are real live scrapes; only `billiejeankingcup` is wired. Don't reintroduce simulated runs.
- A `Proxy.address` may embed credentials — **never render the raw `address`**, always use `display_address`; scrapers must never log it. (The masking regex uses a literal bullet char + `\g<1>`/`\g<2>` group refs; a `\u2022` escape in the replacement template raises "bad escape \u".)
- Keep brand tokens global in `styles.css` `:root`; scope new-page CSS under that page's root class; use the `--app-*` tokens.

## Pointers

- See the `pnpm-workspace` skill for monorepo structure (note: this artifact is now Python, not a pnpm package).
