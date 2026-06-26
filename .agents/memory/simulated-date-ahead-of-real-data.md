---
name: Simulated container date is ahead of real-world data
description: Why live scrapers return empty (200, 0 rows) for "current" date windows in this repo, and how to validate them correctly.
---

# Container "today" runs ahead of the real world

The Replit container's wall-clock "today" is simulated and sits well in the future
(e.g. ~2026-06) relative to the actual real-world date. Live sports/tennis sites only
publish data up to the *real* present, so any scraper date window at or after the
simulated "today" hits dates that don't exist yet in the upstream data.

**Symptom:** the finder/search endpoint returns **HTTP 200 with an empty body
(len 0) and 0 tournaments/rows, 0 errors**, no anti-bot challenge. This looks like a
wiring bug or a silent block but is **neither** — it's just a future date range.

**Why:** confirmed by diffing windows against one upstream (te.tournamentsoftware.com
DoSearch): a 2024-03 / 2025-03 window returned real list items; the 2026-06 window
returned status 200 / len 0. Same code, only the dates differed.

**How to apply:** when validating any live scraper end-to-end, use a clearly *past*
real-world window (e.g. 2024-03) — never the container's "current" year. A 0-row result
on a future window is expected and must not be treated as a regression. (Distinct from
honest-fail-on-block, which shows a challenge/403/timeout, and from the
verifying-background-scrape-runs lifecycle notes.)
