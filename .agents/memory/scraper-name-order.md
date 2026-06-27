---
name: Player name order (Lastname, Firstname)
description: Why every live scraper must emit player names as "Lastname, Firstname" and how to do it deterministically.
---

All live scrapers must emit player names in **`"Lastname, Firstname"`** order
(last name, comma, space, first name).

**Why:** the production source scrapers fed each raw "full name" (which the
tournamentsoftware.com / college / TS-league sources expose as
**`"Firstname Lastname"`**) through a Claude helper (`format_name_gender_claude`,
aka `ln`) whose prompt returned `{"formatted": "Lastname, Firstname", ...}`. The
MatchMiner port is deliberately deterministic/AI-free and **dropped** that Claude
call — so any port that just passes the scraped name straight through leaks raw
`"Firstname Lastname"` (this was the reported "denmark is wrong" bug). Confirmed
from real DB data: `croatia_league` had stored `['Blaž Rola','Rafael Behr',...]`.

**How to apply:** use the shared helper `accounts/live_scrapers/_names.py`
`last_first(raw)` at the player-name **output** chokepoint (not at the lookup/
matching layer — internal joins must keep using the raw name). It is comma-guarded
(a name already containing a comma is treated as already-formatted and only
spacing-normalized, never re-reversed), so it is safe to apply even to sources
that may already emit `"Last, First"`. Single-token / empty names pass through.

- Engines that needed it and now call it: `_ts_tournament._parse_player`,
  `_ts_league._parse_player`, `estonia_tournament._build_row`,
  `itf_juniors_tournament_software._build_row`, `prestosports._build_row`.
- Already correct with their **own** equivalent (do not double-wrap):
  `brazil_results`/`uruguay_results` (`_last_first`), `padelfip`/`usta_team_captains`
  (`_format_name`), and the rankings family (`wtatennis`/`atptour`) + MaxPreps/NJ/
  Stadion/Australia/Ioncourt which build the name from already-separated last/first
  fields. Czech/Poland/Belgium handle source-specific surname-first order
  (`"Lastname Firstname"`): they use their OWN reorder where the **leading**
  tokens are the surname (NOT the shared `last_first`, which treats the last
  token as surname and would reverse them wrongly). `belgium_results._last_first`
  is correct on compound Flemish surnames (`De Wolf Pieter`→`De Wolf, Pieter`,
  `Van Opstal Alex`→`Van Opstal, Alex`); the source's `parts[0]` heuristic is
  worse here. **Gotcha:** belgium's ranking suffix must be stripped from the
  first `" - "` to end (general `r"\s*-\s.*$"`, mirroring the source's
  `correct_name`) — a parens-only regex leaks `"- 35, ptn"` /
  `"- 100 ptn nr., 207"` and mangles the name (e.g. `"Goranov Alexandar - 35,, ptn"`).

**Known limitation (intentional):** the deterministic heuristic treats the last
whitespace token as the surname, so compound/particle/Hispanic surnames are split
worse than Claude did (e.g. "Jan van der Berg" → "Berg, Jan van der"). Accepted
because it matches every other project heuristic; revisit only if a source needs
better. **When porting any new scraper, apply `last_first` to its emitted player
names** unless the source already separates surname/first name.
