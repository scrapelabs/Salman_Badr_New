---
name: Belgium (Tennis Vlaanderen) Zenedge captcha honest-fail
description: Why belgium_results can be wired + deterministic yet fail live on Replit, and what infra a live run needs.
---

# belgium_results — Zenedge captcha gating

`belgium_results` scrapes `www.tennisenpadelvlaanderen.be` (Tennis & Padel
Vlaanderen / Flanders). The site fronts pages with a **Zenedge** anti-bot
interstitial: a normal GET can return an HTML challenge page (POSTs an image
captcha to `/__zenedge/c`) instead of the real content.

## The deterministic port is complete; the captcha is the only blocker
- The parser (`Parser.parse_match`) is fully deterministic and validated against
  synthetic singles + doubles game-table fixtures (no network). It reuses the
  shared 61-col items schema.
- The source's AI name/gender guessing and its player DB cache were **dropped**:
  player gender falls back to the draw's gender (Heren→Male / Dames→Female), and
  `third_party_id` uses the profile-page id with a stable `sha256_id(name)`
  fallback (same convention as estonia/ioncourt).

## A live run needs TensorFlow + the captcha model — NOT vendored
- The solver (`_belgium_captcha.CaptchaSolver`) lazily imports
  `tensorflow.keras` / numpy / PIL and loads a Keras model from
  `accounts/live_scrapers/belgium_assets/captcha_model.keras`.
- Only `belgium_assets/char_map.json` (charset + geometry) is committed. The
  ~42MB model is **not** committed (it's uploaded — see below). TensorFlow IS
  now a wired dep: `tensorflow-cpu==2.15.1` (Linux, pyproject+uv.lock) /
  `tensorflow==2.15.1` (Windows/macOS, requirements.txt platform markers) +
  `pillow`. **Pinned to 2.15** because the model is a tf.keras 2.x `.keras`
  file (the `_patch_batchnorm` shim assumes Keras 2 layer init) — a Keras-3
  (TF ≥2.16) build is a different loader and risks an incompatible load.
- Without them the solver raises `CaptchaSolverUnavailable` → the runner records
  **one** diagnostic error, sets `solver=None`, challenged pages return `""` →
  0 rows → honest `FAILED`. Exactly like the Stadion family without a proxy: a
  missing-infra honest-fail, **not** a wiring bug.

## The model can be UPLOADED (no committed binary)
- A superuser uploads `captcha_model.keras` from the Lab's **Settings** tab. The
  panel only renders for scrapers whose registry spec sets `model_upload_label`
  (`ScraperSpec.model_upload_label` / `model_filename`) — belgium is the only one
  so far.
- Bytes live in Postgres: `ScraperModelFile` (one-to-one with `Scraper`, blob in
  `data=BinaryField`). **Always `.defer("data")`** when you don't need the blob,
  so listing/lookup never drags ~43 MB into memory.
- The worker calls `_belgium_captcha.materialize_uploaded_model(scraper, log)`
  *before* constructing `CaptchaSolver`; it writes the DB blob to `MODEL_PATH`
  only when the on-disk file is missing or differs by size+sha256 (so a locally
  committed/dropped model is left untouched, and re-runs are no-ops). The
  filesystem loader is unchanged — it still reads `MODEL_PATH`.
- **Why DB not filesystem-only:** the hosted filesystem is ephemeral/per-deploy
  and web+worker are separate processes; the DB is the shared durable source of
  truth (same reason runs stream cross-process via `DATABASE_URL`).
- Upload guard: admin-only (mirrors the settings-tab `is_superuser` check),
  ext allow-list `.keras/.h5/.hdf5`, magic-byte sniff (zip `PK\x03\x04` /
  HDF5 `\x89HDF`), 100 MB cap, sha256 recorded. Remove = a separate hidden
  `#removeModelForm` carrying the `#mmConfirm` `data-confirm*` attrs (the confirm
  modal binds to `form[data-confirm]` submit, never to buttons).
- TF (tensorflow-cpu 2.15.1) is now a wired dependency, so an uploaded model
  loads and the solver runs. Verified on Replit with a synthetic load+infer test
  (build a 6-head × 62-class `.keras`, load it via `CaptchaSolver._ensure_model`,
  predict on a blank PNG). That proves the *plumbing*; the user's actual model
  loading under 2.15 is still confirmed by a real run (a version mismatch would
  surface as the honest-fail "captcha model/char-map could not be loaded: …").

## To run it live (two options)
1. **Hosted/prod:** TF is already wired (deploy picks up `tensorflow-cpu` from
   `pyproject.toml`+`uv.lock`); just upload `captcha_model.keras` via the Settings
   tab (or drop it into `belgium_assets/`). Note Zenedge may *also* block
   datacenter IPs (like CloudFront does to Stadion), so a residential proxy may
   still be required even with a working solver.
2. **Local Windows:** `git pull`, re-run `bat_files/0_setup.bat` (now installs
   `tensorflow==2.15.1` via `requirements.txt`), upload the model via Settings (or
   place it in `belgium_assets/`), then run
   `bat_files/11_scrape_belgium_results.bat` (URL or date-range prompt).

**Why honest-fail instead of stubbing:** never fabricate rows. A source whose
infra (proxy / creds / model) isn't present must emit a correct empty 5-tuple +
errors CSV, consistent with the rest of the catalogue.
