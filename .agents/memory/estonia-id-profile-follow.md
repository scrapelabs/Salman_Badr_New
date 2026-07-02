---
name: Estonia tournament third-party id — profile-follow
description: Why Estonia player licence ids go missing on legacy/team tournaments and how the id must be resolved
---

# Estonia (`etl.tournamentsoftware.com`) player licence id resolution

The licence id (third_party_id) lives in TWO different places depending on the
page, and reading only one place silently loses ids on whole tournaments.

- **Modern player/match page** — id is in the `page-subhead` h4
  `media__title-aside` (parenthesised, e.g. `(10089705)`). Present on
  singles/league match pages (e.g. SEB Liigatennis).
- **Legacy / team player pages** (e.g. Klubide Karikavõistlused, the clubs'
  cup) — the match page does **NOT** show the id in the subhead aside. The id
  only lives on the player's **profile page**, reached by following the profile
  link (`page-subhead h4 media__title/a/@href`, or the legacy
  `#content .subtitle h2 a.button/@href`) and reading `page-head` h2
  `media__title-aside`.

**Symptom of the bug:** "many players missing third party ID" on some
tournaments while others are fine. Reproduced: the 447-player club cup had
~100% empty match-page asides but 100% recoverable from the profile page; a
singles league had 0 missing (its match pages carry the aside).

**Rule:** `_player_id` must try the match-page subhead aside first, then FOLLOW
the profile link and read the profile's page-head h2 aside, and only then fall
back to `sha256_id` (which is blanked at export). This mirrors the source's
`DetailsSportParser.parse_page_profile` (players.py) — the modern
`DetailsTournamentParser` alone does not follow the profile for the id, which is
why an id-from-match-page-only port under-collects on legacy/team draws.

**Why it's cheap:** stage B already fetches the profile page for DOB
(`_player_dob`), and `_get_sel` caches per URL, so the profile-follow reuses the
cached page. On modern tournaments the subhead aside hits first, so no extra
fetch happens (no regression).

**Genuinely blank is faithful:** guest/unlicensed players have no profile link
at all (`href` absent) and no id anywhere — the source sha256→blanks them too.
A small residual of named-but-blank ids on a modern draw is expected, not a bug.
