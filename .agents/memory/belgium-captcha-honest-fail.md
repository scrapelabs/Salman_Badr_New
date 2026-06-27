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
  ~42MB model and TensorFlow are deliberately **not** installed/committed
  (far-reaching: huge dep + large binary).
- Without them the solver raises `CaptchaSolverUnavailable` → the runner records
  **one** diagnostic error, sets `solver=None`, challenged pages return `""` →
  0 rows → honest `FAILED`. Exactly like the Stadion family without a proxy: a
  missing-infra honest-fail, **not** a wiring bug.

## To run it live (two options, ask the user)
1. **Hosted/prod:** install TensorFlow (add to pyproject + requirements.txt) and
   drop `captcha_model.keras` into `belgium_assets/`, then run. Note Zenedge may
   *also* block datacenter IPs (like CloudFront does to Stadion), so a
   residential proxy may still be required even with a working solver.
2. **Local Windows:** `git pull`, place the model in `belgium_assets/`, add TF to
   the local venv, run via `bat_files/11_scrape_belgium_results.bat` (URL or
   date-range prompt).

**Why honest-fail instead of stubbing:** never fabricate rows. A source whose
infra (proxy / creds / model) isn't present must emit a correct empty 5-tuple +
errors CSV, consistent with the rest of the catalogue.
