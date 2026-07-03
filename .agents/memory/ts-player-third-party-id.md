---
name: tournamentsoftware player third-party id
description: On TS tournament crawls the requested player URL is tournament-scoped; the real player id is the subhead profile-link GUID, not the requested URL and not the member-id aside.
---

For tournamentsoftware individual-tournament scrapers (the `_ts_tournament`
engine), the crawl reaches each player through a **tournament-scoped** URL of the
form `/sport/player.aspx?id=<TOURNAMENT_GUID>&player=<n>`. The `id` query there is
the **tournament's** GUID — identical for every player in that event. Never derive
a player's `third_party_id` from that requested URL.

The genuine per-player tournamentsoftware id is the **subhead player-profile
link** `/player-profile/<GUID>` (the same href the `ranking_dob` DOB join already
reads). The `media__title-aside` next to the name is a **national-federation
member id** (e.g. `AMI7610273`), NOT the platform id.

**Rule:** for sites whose framework `third_party_id` should be the platform GUID
(junior sites like Tennis Europe — config flag `guid_third_party_id`), take it
from the subhead `profile_href`, upper-cased to the canonical form the site's
address-bar profile URLs use. Emit `""` when the profile page fails or the subhead
has no profile link — never fall back to the tournament GUID. Default the flag off
so the other ~20 TS scrapers keep the member-id-aside behaviour unchanged.

**Why:** a first attempt parsed the GUID from the *requested* URL and shipped the
tournament GUID for every player (worse than the pre-existing member id). Verified
live past the cookiewall: `player.aspx?id=c1462ce0-…&player=87` → subhead
`profile_href=/player-profile/c33ba4c7-…` → emit `C33BA4C7-…`. The `_guid_from_profile_url`
helper honours a `?id=` query only when the path contains "profile", so a
`player.aspx?id=` URL can't leak the tournament id.
