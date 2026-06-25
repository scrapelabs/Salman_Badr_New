---
name: Porting source scrapers into MatchMiner
description: Cross-cutting cautions when porting the attached_assets script catalogue into LIVE_SCRAPERS (probe the live API, the sources contain real bugs, credential handling).
---

# Porting the source-script catalogue (attached_assets scripts zip)

These ~38 source scrapers are being ported one-by-one into `accounts/live_scrapers/`.
They were written against the user's private framework and **contain real bugs** plus
cosmetic AI steps. Treat the source as a reference, not gospel.

## Always probe the live API/page before trusting source field names
The sources read keys that no longer exist or never did. Confirm every join key and
output field against a live response first.
**Why:** e.g. ioncourt read `match.get('matchId')` but the real field is `_id`; since the
row dedup key was `matchId`, every match collapsed to ONE deduped row — the source as
written returns almost nothing. A 2-minute probe caught it.
**How to apply:** hit login → list → detail once with curl_cffi (impersonate chrome) and
diff the actual JSON/HTML keys against what the source reads. Pay special attention to
**join keys across endpoints** — ids often differ per endpoint (ioncourt: the detail/info
side's `participant.person._id` equals the match endpoint's `participant._id`, NOT the
info `participant._id`).

## Drop the AI, keep it deterministic
The AI calls in these sources are cosmetic (gender/name/"official college name" guessing).
Replace with deterministic logic: e.g. the college-name "officializer" becomes
`re.sub(r'\(.*','',raw).strip()` (strip the `(M)/(W)` suffix). No OpenAI/Anthropic.

## Credentialed scrapers: env + honest-fail + in-process validation
Some sources hard-code logins. Never commit them. Read via
`getattr(settings, 'XXX_PHONE'/'XXX_PASSWORD', '')` (add empty placeholders to root
`.env.example`); if unset, **fail honestly** (FAILED + error CSV), exactly like a Stadion
scraper without its proxy. Request the real values as Replit secrets at report time, don't
block mid-port.
**Validate without leaking/committing:** call `runner.run(run_obj, log)` **in-process** in
`manage.py shell` (NOT the worker subprocess — it dies at bash teardown), injecting creds
read at runtime from the gitignored source file (`tmp/scripts_new/...`) into
`settings.XXX_*`. Use a tiny recent window, then assert the creds do not appear anywhere in
items/requests/errors CSV or the log lines.

## Reuse the shared 61-col items schema
Reuse `brazil_results.COLUMNS` verbatim (it's 61 cols despite the "60-col" shorthand in
replit.md) so every scraper's downloadable items CSV stays uniform. Map source-specific
extras onto existing columns rather than adding new ones (ioncourt's team identity rides in
`*_college` + the embedded `tournament_name`).

## Date-range sources may only reach RECENT windows
ioncourt's tie search is date-desc with a cumulative "out-of-window counter" guard
(stop after ~60 out-of-window ties). That means windows far in the past return 0 (paging
trips the guard before reaching them). Faithful to source; only validate with recent dates.
