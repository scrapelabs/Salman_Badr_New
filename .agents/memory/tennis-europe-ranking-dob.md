---
name: Tennis Europe biography DOB
description: Where tennis_europe DOB comes from (Biography tab, full coverage), why the old ranking-tab registry was wrong, and the profile-GUID identity/cache that make it work.
---

Tennis Europe player DOB is sourced from each player's **Biography tab**
(`/player-profile/<GUID>/biography` → the "Year of birth" dt/dd), read lazily and
cached per run by profile GUID. Config flag: `biography_dob=True` (not
`ranking_dob`). Gender is independent (Claude name inference).

## Why biography, not the ranking tab
- The site's Biography tab exposes "Year of birth" for **every** player — ranked
  AND unranked — verified live (a ranked player, an unranked entrant blank in the
  old CSVs, and players that previously showed garbage all returned a correct YOB).
  So per-player DOB gives **full coverage**, ~12-year-olds in a "Sub12"/U12 draw
  correctly resolve to their birth year.
- The old `ranking_dob` registry was **doubly wrong**:
  1. `/ranking/` lists **two** rankings — a **singles** ranking (`rid=79`: columns
     …Player[td4], **Year of birth**[td5], Points[td6]…) and a **doubles** ranking
     (`rid=157`: columns …Player[td4], **Points**[td5], Total, Tournaments, Country —
     **no "Year of birth" column**). `_ranking_rows`' non-full-date branch blindly
     read `td[5]` on both, so doubles **points** (e.g. 549/474/894, even 4-digit)
     were recorded as `1/1/<points>` — the "garbage DOB" bug.
  2. `dob_map.update()` ran `rid=157` after `rid=79`, so a player in both rankings
     had their correct singles YOB **overwritten** by their doubles points.
  3. Coverage was capped at the server-reachable ranked population (~40/category),
     so most (unranked) entrants stayed blank — the "mostly empty DOB" bug.
- The stale prior claim that "junior profiles/biography hide YOB" was **false**
  (or the site changed): the Biography tab has it for everyone.

## The identity / cache that makes it work
- `_parse_player` reads the profile GUID from the tournament-scoped page's
  **subhead** `/player-profile/<GUID>` link (never from the requested
  `/sport/player.aspx?id=<TOURNAMENT_GUID>` URL — those are different GUIDs).
- That GUID keys a per-run cache (the shared `dob_map`, passed into crawl ctx when
  `ranking_dob OR biography_dob`), so each unique player's biography — including a
  blank "" **negative** result — is fetched at most once even though players recur
  across many matches. No response caching exists in `ScraperClient`, so this
  GUID cache is what keeps request volume bounded to unique-players.
- Cookiewall warmup is still required before any TE page returns rows.

## COSAT is separate — do not conflate
- COSAT uses `ranking_dob=True` + `ranking_dob_full_date=True`: a genuinely
  different layout (More links on the `/ranking/` index, profile link in `td[5]`,
  a **full** `m/d/Y` DOB in `td[6]`). It needs the ranking path because biography
  only gives a year; COSAT stores a full date. The `_ranking_rows` /
  `_ranking_dob_seed` **full-date** branches still serve COSAT and were left intact.
  The now-unused non-full-date branch is the one that had the td[5] bug.
