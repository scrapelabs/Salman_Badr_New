---
name: Tennis Europe ranking-tab DOB
description: Where tennis_europe DOB comes from, why coverage is ranked-juniors-only, and the GUID-join identity that makes it work.
---

Tennis Europe player DOB is sourced from the site-wide **ranking tab**, joined to
match players by profile GUID. Gender is independent (Claude name inference, works
for every player); DOB is the fragile part.

## The site moved the full listings to JS
- `category.aspx?...&ps=100` (the paginated FULL ranked population) is now
  **client-side rendered** — the server-side `table.ruler` is an empty shell over
  plain HTTP, so it yields zero rows. This is why the DOB registry silently went
  to `{}` and every join returned blank (the original "not picking DOB" bug).
- Server-rendered fallback that still returns real rows:
  `ranking.aspx?id=<pub>&category=<cat>&ps=100`. **But** it only serves the
  **top ~40 per category** and **ignores `&p=N` paging** — there is no server-side
  way to page deeper.
- The `/ranking/` index lists only **2 rankings** (14U / 16U), **4 categories
  each** → about **63 unique** ranked juniors with a YOB. That is the *maximum*
  server-reachable DOB population.

## The join identity (why it works)
- Ranking rows expose the player as `profile/default.aspx?id=<GUID>`.
- A match player's tournament-scoped page (`/sport/player.aspx?id=<TOURNAMENT_GUID>`)
  carries the canonical `/player-profile/<GUID>` link in its **subhead**.
- These two GUIDs are the **same tournamentsoftware player GUID**; both sides
  lowercase it, so `dob_map.get(guid)` fires. `_parse_player` must read the GUID
  from the **subhead profile link**, never from the requested tournament-scoped URL.

## Testing gotcha
- A player's **own** `/player-profile/<GUID>` page has a *different* subhead (the
  title is plain text, not an `<a>`), so probing the join via a canonical URL gives
  a **false MISS**. Always test with a real tournament-scoped `/sport/player.aspx?id=…`
  URL (e.g. off another player's match, or the target's own participation links).

## Consequence / contract
- DOB lands **only for currently-ranked juniors**; unranked match players keep a
  blank DOB — the documented "no fallback" contract, not a bug. Random entrant
  sampling will mostly miss (63 registry vs. thousands of entrants).
- **Why it can't be wider today:** the site, not the code, dropped server-side full
  listings. Broader coverage would need a **headless render** of `category.aspx`
  (patchright, like `college_dual_match` uses `_browser.py`) — a follow-up, not a fix.
- COSAT (`ranking_dob_full_date=True`) is a *different* layout (More links on the
  index, server-rendered paginated `category.aspx`, full DOB in td[6]) and is
  unrelated to the TE path.
