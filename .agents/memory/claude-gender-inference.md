---
name: Claude name→gender inference (TS engines)
description: How the Croatia tournament/league scrapers infer player + draw gender via cached Claude calls, and the mixed-draw / genderless-name rules that must hold.
---

# Claude name→gender inference

Some tournamentsoftware (TS) sources — notably the **Croatia** tournament & league
scrapers — have draw names that carry **no gender word** (e.g. "Prva liga"), so the
deterministic draw-name heuristic (`_gender.draw_gender_code`) can't label them.
For those, gender is inferred from **player names** via Claude.

**Opt-in, not global.** The shared TS engines gate this behind a `claude_gender`
config flag (default `False`); only scrapers that set it True call Claude. Other TS
scrapers are unaffected. As of July 2026 every opted-in TS scraper (Croatia
tournament & league, Finland tournament & league, Tennis Europe) also sets
`claude_gender_required=True` — **HARD** mode: no key → honest-fail before any
network, never fall back to draw-name gender for players (user directive; soft
mode remains only as an unused engine capability).

**Rule — draw_gender precedence in `_build_row`:**
1. explicit gender token in the draw name wins;
2. **mixed draws → blank** (never fall back to a player's gender);
3. otherwise fall back to **winner_1's** inferred gender.
Per-player gender is **always** resolved individually (so mixed doubles get correct
w1/w2 genders while the row-level draw_gender stays blank).

**Why the mixed-draw guard matters / the trap:** Croatian inflects the adjective
("mješovit**i**", "mješovit**a**", "mješovit**o** parovi"). A whole-word regex like
`\bmjesovit\b` **fails** on those because the trailing letter defeats the `\b`, so a
real mixed draw would be misdetected as non-mixed and wrongly take the winner's
gender. `_gender._MIXED_RE` must match the **stem + `\w*`**, not a fixed word.
**How to apply:** when adding mixed tokens for a new language, add distinctive
stems (accent-stripped, since names are normalized first), and verify
`is_mixed_draw()` against real inflected forms before trusting it.

**Caching (`PlayerGenderCache` + `_claude_gender.resolve_gender`):**
- keyed by a normalized name; concurrent cold-cache misses may each call Claude once
  before one write wins (accepted tradeoff — `get_or_create` tolerates the race);
- **transient Claude errors return None and are NOT cached** so they retry next run;
  only a definitive answer (incl. unknown → blank) is persisted.

**Why model id drifts:** the container clock is mid-2026, so older Haiku ids (e.g.
`claude-3-5-haiku-20241022`) **404**. Use the current lineup default and keep it
overridable via `settings.CLAUDE_GENDER_MODEL`. If gender silently comes back empty,
check for a 404 on the model id first.

**How to verify without a full run:** league runs outlive the bash timeout (Claude
latency compounds it). Verify `resolve_gender` + the draw_gender branches with a
short shell probe (unit-style), and drive a single discovered **tournament** through
`_discover_range`→`_list_players`→`_parse_player_matches` for a real end-to-end check.
